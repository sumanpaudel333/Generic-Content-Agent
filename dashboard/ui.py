"""
Shared UI chrome for the Content and Automation Dashboard.

Holds the theme, the page shell, and the navigation -- kept separate from
app.py so adding a new automation is a matter of registering it in MODULES
and adding its routes, without touching layout or styling.

Navigation is a left sidebar rather than a row of tabs across the top. The
list had grown to seven entries, which wrapped onto a second line on a laptop
and pushed the page content down; going vertical gives each entry room for an
icon and a readable label, keeps every section visible at once, and leaves the
full width of the window for the work itself.

Theme is BC Sands blue with a yellow accent.
"""
import html
from typing import Any

APP_NAME = "Content and Automation"
APP_SUBTITLE = "Dashboard"
# What follows the page name in the browser tab.
TITLE_SUFFIX = "Content and Automation Dashboard"

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
        "icon": "doc",
        "blurb": "Writes product descriptions for you and puts each one in a queue "
                  "to be checked. Nothing reaches the website until someone here "
                  "approves it.",
    },
    {
        "key": "chat-insights",
        "label": "Chat Insights",
        "path": "/chat-insights",
        "status": "live",
        "icon": "chat",
        "blurb": "Reads the week's website chats and tells you what customers asked "
                  "about, which questions the chatbot could not answer, and who left "
                  "their details wanting a call back.",
    },
    {
        "key": "site-health",
        "label": "Site health",
        "path": "/site-health",
        "status": "live",
        "icon": "pulse",
        "blurb": "Checks the online shop every five minutes and emails you when a "
                  "page goes down, slows down or shows an error.",
    },
    {
        "key": "pricing-agent",
        "label": "Pricing Agent",
        "path": "#",
        "status": "planned",
        "icon": "tag",
        "blurb": "Not built yet. This space is reserved for the next automation.",
    },
]

NAV_ITEMS = [{"key": "overview", "label": "Overview", "path": "/", "icon": "grid"}] + [
    {"key": m["key"], "label": m["label"], "path": m["path"], "icon": m.get("icon", "")}
    for m in MODULES if m["status"] == "live"
]

# The admin-only end of the nav. These three all live under /settings, so they
# cannot be highlighted by module -- they are matched against the current URL
# instead (see _nav_html). Registering them here keeps that matching in one
# place as more settings pages arrive.
ADMIN_NAV_ITEMS: list[dict[str, str]] = [
    {"key": "settings-users", "label": "People", "path": "/settings/users", "icon": "users"},
    {"key": "settings-jobs", "label": "Job history", "path": "/settings/jobs", "icon": "jobs"},
    {"key": "settings-audit", "label": "Activity log", "path": "/settings/audit", "icon": "audit"},
    {"key": "settings-images", "label": "Photo storage", "path": "/settings/images", "icon": "image"},
]

# Line-art glyphs for the sidebar. Inline rather than a font or sprite sheet:
# there are eight of them, they inherit currentColor so the active state needs
# no second copy, and it keeps the page a single request.
ICONS = {
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/>'
             '<rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/>',
    "doc": '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>'
            '<path d="M13 2v7h7"/><path d="M8 13.5h8M8 17.5h5"/>',
    "chat": '<path d="M20.5 11.5a8 8 0 0 1-8.6 8 8.7 8.7 0 0 1-3.7-.9L3.5 20.5l1.9-4.7a8 8 0 0 1-1.9-5.2 8 8 0 0 1 8.1-7.6h.4a8 8 0 0 1 7.6 7.6z"/>',
    "tag": '<path d="M20.6 13.4 12.9 21a1.5 1.5 0 0 1-2.1 0l-7.3-7.3V3.5h10.2l6.9 6.9a1.5 1.5 0 0 1 0 2z"/>'
            '<circle cx="7.6" cy="7.6" r="1.4"/>',
    "users": '<path d="M15.5 20.5v-1.8a3.6 3.6 0 0 0-3.6-3.6H6.1a3.6 3.6 0 0 0-3.6 3.6v1.8"/>'
              '<circle cx="9" cy="7.5" r="3.6"/><path d="M21.5 20.5v-1.8a3.6 3.6 0 0 0-2.7-3.5"/>'
              '<path d="M16 4.1a3.6 3.6 0 0 1 0 7"/>',
    "jobs": '<path d="m4.5 16.5 5-5-5-5"/><path d="M12 18.5h7.5"/>',
    "audit": '<path d="M12 21.5s7.5-3.8 7.5-9.5V5.2L12 2.5 4.5 5.2V12c0 5.7 7.5 9.5 7.5 9.5z"/>'
              '<path d="m9.2 11.8 2 2 3.6-3.7"/>',
    "image": '<rect x="3" y="3.5" width="18" height="17" rx="2.2"/><circle cx="8.6" cy="9.1" r="1.6"/>'
              '<path d="m21 15.5-4.6-4.4L5 20.5"/>',
    "arrow": '<path d="M5 12h13"/><path d="m12.5 5.5 6.5 6.5-6.5 6.5"/>',
    "pulse": '<path d="M3 12h4.2l2.4-6.2 4.2 12.4 2.4-6.2H21"/>',
    "phone": '<path d="M21.5 16.9v2.6a1.8 1.8 0 0 1-2 1.8 17.6 17.6 0 0 1-7.7-2.7 17.3 17.3 0 0 1-5.3-5.3'
              'A17.6 17.6 0 0 1 3.8 5.5a1.8 1.8 0 0 1 1.8-2h2.6a1.8 1.8 0 0 1 1.8 1.5c.1.9.3 1.7.6 2.5a1.8 1.8 0 0 1-.4 1.9'
              'l-1.1 1.1a14 14 0 0 0 5.3 5.3l1.1-1.1a1.8 1.8 0 0 1 1.9-.4c.8.3 1.6.5 2.5.6a1.8 1.8 0 0 1 1.6 1.9z"/>',
}


def icon(name: str, size: int = 18) -> str:
    """One glyph, or nothing if the name is unknown."""
    path = ICONS.get(name or "")
    if not path:
        return ""
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{path}</svg>')

# Where the "Settings" breadcrumb points -- the admin landing page.
SETTINGS_ROOT = "/settings/users"


def path_matches(current_path: str, item_path: str) -> bool:
    """True when current_path is item_path itself or a page beneath it.

    Prefix matching is deliberately segment-aware: /settings/jobs must not
    light up for /settings/jobsomething.
    """
    if not current_path or item_path in ("", "#"):
        return False
    item_path = item_path.rstrip("/") or "/"
    return current_path == item_path or current_path.startswith(item_path + "/")


def module_by_key(key: str) -> dict | None:
    return next((m for m in MODULES if m["key"] == key), None)


# Served by the /assets mount in dashboard/app.py, straight from the repo's
# assets/ folder -- one copy of the logo, no duplicate in the package.
LOGO_URL = "/assets/BC-Sands-Logo.png"


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
THEME_CSS = """
:root {
    --blue-900:#00224E; --blue-800:#003370; --blue-700:#004495; --blue-600:#0A5FBF;
    --blue-500:#1E7AE0; --blue-100:#DCE9F8; --blue-50:#F0F6FD;
    --yellow-600:#E0A500; --yellow-500:#FFC72C; --yellow-300:#FFDE7A; --yellow-100:#FFF6DA;
    --ink:#101A2B; --muted:#5D6E88; --line:#E3EAF3; --bg:#F5F8FC; --card:#FFFFFF;
    --green:#0E8A4A; --green-bg:#E7F7EE; --red:#C62828; --red-bg:#FDEDED;
    --amber:#B26B00; --amber-bg:#FFF7E6;
    --sidebar:250px;
    --radius:12px;
    /* Two shadows rather than one: a tight contact shadow keeps an edge
       readable against the page, the wide one gives the lift. */
    --shadow:0 1px 2px rgba(16,26,43,.05), 0 6px 20px rgba(16,26,43,.06);
    --shadow-lg:0 2px 4px rgba(16,26,43,.06), 0 16px 40px rgba(16,26,43,.11);
    --ease:cubic-bezier(.4,0,.2,1);
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { font-family:'Segoe UI',-apple-system,system-ui,'Helvetica Neue',Arial,sans-serif; margin:0;
       background:var(--bg); color:var(--ink); font-size:14px; line-height:1.55;
       -webkit-font-smoothing:antialiased; }
a { color:var(--blue-600); }
::selection { background:var(--yellow-300); color:var(--blue-900); }

/* Keyboard users get a visible ring everywhere; mouse users never see it. */
:focus-visible { outline:2px solid var(--blue-500); outline-offset:2px; border-radius:4px; }

/* Anything that scrolls inside the page gets a slim, quiet scrollbar rather
   than the platform's full-width one. */
.nav::-webkit-scrollbar, .logview::-webkit-scrollbar { width:8px; height:8px; }
.nav::-webkit-scrollbar-thumb { background:rgba(255,255,255,.22); border-radius:8px; }
.logview::-webkit-scrollbar-thumb { background:#334155; border-radius:8px; }

/* A link for keyboard and screen-reader users to jump the navigation. Off
   screen until focused, which is the only time it is useful. */
.skip { position:absolute; left:-9999px; top:0; z-index:300; background:var(--blue-700);
        color:#fff; padding:10px 16px; border-radius:0 0 10px 0; font-size:13px; }
.skip:focus { left:0; }

/* ---------- Shell ----------
   A fixed sidebar and a scrolling column beside it. The sidebar is fixed
   rather than sticky so a long review queue scrolls under it without the
   navigation ever leaving the screen. */
.shell { display:flex; min-height:100vh; }
.content { flex:1; min-width:0; margin-left:var(--sidebar); display:flex; flex-direction:column; }

/* ---------- Sidebar ----------
   The logo is a transparent PNG whose wordmark is black and blue, so it cannot
   sit directly on the blue panel. It gets a white chip, which is how the brand
   appears on dark backgrounds anyway. */
.sidebar { position:fixed; top:0; bottom:0; left:0; width:var(--sidebar); z-index:60;
           display:flex; flex-direction:column;
           background:linear-gradient(168deg,var(--blue-900) 0%,var(--blue-800) 48%,var(--blue-700) 100%);
           color:#fff; box-shadow:1px 0 0 rgba(16,26,43,.08);
           transition:transform .28s var(--ease); }
/* A hairline of brand yellow down the inside edge, in place of the keyline the
   old top bar carried at its foot. */
.sidebar::after { content:""; position:absolute; top:0; right:0; bottom:0; width:2px;
                  background:linear-gradient(180deg,var(--yellow-500),rgba(255,199,44,0) 62%); }

.brand { display:flex; align-items:center; gap:12px; padding:19px 18px; color:#fff;
         text-decoration:none; border-bottom:1px solid rgba(255,255,255,.09); }
.brand-logo { background:#fff; border-radius:10px; padding:6px 9px; display:grid; place-items:center;
              box-shadow:0 2px 8px rgba(0,0,0,.22); flex:none; }
.brand-logo img { display:block; height:26px; width:auto; }
.brand-text { display:flex; flex-direction:column; line-height:1.2; min-width:0; }
.brand-title { font-weight:650; font-size:13.5px; letter-spacing:.1px; }
.brand-sub { font-weight:500; font-size:10px; opacity:.66; letter-spacing:1.1px;
             text-transform:uppercase; margin-top:2px; }

.nav { flex:1; overflow-y:auto; padding:10px 12px 16px; display:flex; flex-direction:column;
       gap:2px; scrollbar-width:thin; }
.nav-group { font-size:9.5px; font-weight:700; text-transform:uppercase; letter-spacing:1.2px;
             opacity:.5; padding:16px 12px 7px; }
.nav a { display:flex; align-items:center; gap:11px; padding:9px 12px; border-radius:9px;
         color:rgba(255,255,255,.78); text-decoration:none; font-size:13.5px; font-weight:500;
         position:relative; transition:background .16s var(--ease), color .16s var(--ease); }
.nav a svg { flex:none; opacity:.85; transition:opacity .16s; }
.nav a:hover { background:rgba(255,255,255,.1); color:#fff; }
.nav a:hover svg { opacity:1; }
.nav a.active { background:#fff; color:var(--blue-800); font-weight:650;
                box-shadow:0 2px 12px rgba(0,0,0,.2); }
.nav a.active svg { opacity:1; color:var(--blue-600); }
/* The yellow tab reads as the page marker; the white pill alone looked like a
   hover state on a dark panel. */
.nav a.active::before { content:""; position:absolute; left:-12px; top:50%; width:3px; height:20px;
                        margin-top:-10px; border-radius:0 3px 3px 0; background:var(--yellow-500); }
.sidebar a:focus-visible, .logout-btn:focus-visible {
    outline:2px solid var(--yellow-500); outline-offset:2px; }

.usermenu { display:flex; align-items:center; gap:10px; padding:13px 16px;
            border-top:1px solid rgba(255,255,255,.09); background:rgba(0,0,0,.14); }
.avatar { width:33px; height:33px; border-radius:50%; background:var(--yellow-500); color:var(--blue-900);
          display:grid; place-items:center; font-weight:700; font-size:12.5px; flex:none; }
.usermenu > div:nth-child(2) { min-width:0; flex:1; }
.uname { font-size:13px; line-height:1.2; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.urole { font-size:9.5px; opacity:.62; text-transform:uppercase; letter-spacing:.7px; }
.logout-btn { background:rgba(255,255,255,.12); color:#fff; border:1px solid rgba(255,255,255,.2);
              padding:6px 11px; font-size:12px; border-radius:8px; cursor:pointer;
              transition:background .16s, border-color .16s; }
.logout-btn:hover { background:rgba(255,255,255,.24); border-color:rgba(255,255,255,.36); }

/* ---------- Topbar ----------
   Holds the trail and, on a narrow screen, the control that opens the drawer.
   Translucent so content scrolling under it stays legible as motion rather
   than disappearing behind a solid block. */
.topbar { position:sticky; top:0; z-index:40; display:flex; align-items:center; gap:12px;
          min-height:54px; padding:9px 26px; background:rgba(245,248,252,.82);
          backdrop-filter:saturate(180%) blur(12px); -webkit-backdrop-filter:saturate(180%) blur(12px);
          border-bottom:1px solid var(--line); }
.burger { display:none; background:var(--card); border:1px solid var(--line); border-radius:9px;
          width:38px; height:38px; place-items:center; cursor:pointer; color:var(--ink);
          flex:none; padding:0; transition:border-color .16s, background .16s; }
.burger:hover { border-color:var(--blue-600); background:var(--blue-50); }
.burger svg { display:block; }
.sidebar-scrim { position:fixed; inset:0; z-index:55; background:rgba(16,26,43,.45);
                 backdrop-filter:blur(2px); opacity:0; transition:opacity .24s var(--ease); }
body.nav-open .sidebar-scrim { opacity:1; }

/* ---------- Breadcrumbs ----------
   One trail per page, directly under the app bar. It replaced the assorted
   "back to ..." links that used to sit at the end of each page's subtitle:
   those only ever went up one level, and you had to hunt for them. */
.crumbs { display:flex; align-items:center; gap:3px; flex-wrap:wrap; font-size:12.5px;
          margin:0; color:var(--muted); min-width:0; }
.crumbs a { color:var(--muted); text-decoration:none; padding:3px 8px; border-radius:6px;
            display:inline-flex; align-items:center; gap:5px; transition:.15s; flex:none;
            max-width:34ch; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.crumbs a:hover { color:var(--blue-700); background:var(--blue-50); }
.crumbs a:focus-visible { outline:2px solid var(--blue-500); outline-offset:1px; }
.crumbs svg { fill:currentColor; flex:none; }
.crumb-sep { color:#B8C4D4; user-select:none; font-size:13px; line-height:1; }
.crumb-current { color:var(--ink); font-weight:600; padding:3px 8px; max-width:46ch; flex:none;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* ---------- Layout ---------- */
.wrap { max-width:1320px; width:100%; margin:0 auto; padding:26px 26px 72px;
        animation:page-in .22s var(--ease); }
/* A short fade on load. Long enough to read as the page settling, short
   enough that nobody waits for it. */
@keyframes page-in { from { opacity:0; transform:translateY(5px); } }
@media (prefers-reduced-motion:reduce) {
    html { scroll-behavior:auto; }
    .wrap { animation:none; }
    .sidebar { transition:none; }
}
.page-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px;
             flex-wrap:wrap; margin-bottom:20px; }
h1 { font-size:23px; margin:0 0 5px; letter-spacing:-.35px; font-weight:680; }
h2 { font-size:16px; margin:0 0 12px; letter-spacing:-.15px; }
.subtitle { color:var(--muted); font-size:13.5px; margin:0; max-width:74ch; }

/* ---------- Sub-tabs ---------- */
.tabs { display:flex; gap:6px; margin-bottom:18px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.tabs a { padding:9px 15px; font-size:13.5px; color:var(--muted); text-decoration:none; font-weight:500;
          border-bottom:2.5px solid transparent; margin-bottom:-1px; transition:.15s; }
.tabs a:hover { color:var(--blue-700); }
.tabs a { transition:color .16s var(--ease), border-color .16s var(--ease); }
.tabs a.active { color:var(--blue-700); border-bottom-color:var(--yellow-500); font-weight:650; }
.tab-count { background:var(--blue-100); color:var(--blue-700); font-size:11px; font-weight:700;
             padding:1px 7px; border-radius:999px; margin-left:6px; }
.tabs a.active .tab-count { background:var(--yellow-500); color:var(--blue-800); }

/* ---------- Stat tiles ---------- */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:13px; margin-bottom:22px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:13px 15px; box-shadow:var(--shadow); border-left:3px solid var(--blue-600); }
.stat { transition:box-shadow .18s var(--ease), transform .18s var(--ease); }
.stat:hover { box-shadow:var(--shadow-lg); transform:translateY(-1px); }
.stat.accent { border-left-color:var(--yellow-500); }
.stat.ok { border-left-color:var(--green); }
.stat.bad { border-left-color:var(--red); }
.stat.warn { border-left-color:var(--amber); }
.stat-label { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); font-weight:600; }
.stat-value { font-size:25px; font-weight:720; margin-top:4px; letter-spacing:-.7px;
              font-variant-numeric:tabular-nums; }
.stat-value.sm { font-size:14px; font-weight:600; margin-top:5px; }
.stat a { text-decoration:none; color:inherit; }
.dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot.ok { background:var(--green); } .dot.bad { background:var(--red); }

/* ---------- People ----------
   A card per person rather than a table row. The row had four columns and a
   pair of controls crammed into the last one; a name, an email address and a
   role are things you edit, and editing them in a table means either a modal
   or a separate page for what is three text boxes. */
.people { display:flex; flex-direction:column; gap:12px; }
.person { border:1px solid var(--line); border-radius:var(--radius); background:var(--card);
          padding:15px 16px; transition:border-color .18s var(--ease), box-shadow .18s var(--ease); }
.person:hover { border-color:#D3DEEC; box-shadow:var(--shadow); }
.person-head { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
               padding-bottom:13px; border-bottom:1px solid var(--line); margin-bottom:13px; }
.avatar.lg { width:40px; height:40px; font-size:14px; background:var(--blue-100);
             color:var(--blue-700); }
.person-id { min-width:0; flex:1; }
.person-name { font-size:15px; font-weight:650; }
.person-state { margin-left:auto; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }

/* The edit row and the "add somebody" form share a shape: labelled boxes that
   wrap, with the button on the end. */
.person-edit { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
.person-edit > div { flex:1 1 190px; min-width:0; }
.person-edit.wide > div { flex:1 1 165px; }
.person-edit label { font-size:12px; font-weight:600; color:var(--muted); }
.person-edit input, .person-edit select { width:100%; margin-top:4px; }
.person-edit button { flex:none; }

.person-actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center;
                  margin-top:13px; padding-top:13px; border-top:1px dashed var(--line); }

/* Deleting sits apart from the rest, below its own line and tinted, so it is
   never the button next to the one you meant to press. */
.person-danger { margin-top:12px; padding-top:11px; border-top:1px solid var(--red-bg); }
.person-delete { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:0; }
.person-delete input { flex:1 1 240px; min-width:0; font-size:12.5px; }
.person-delete button { flex:none; }
@media (max-width:620px) {
    .person-edit > div, .person-edit.wide > div { flex:1 1 100%; }
    .person-edit button, .person-actions form, .person-actions button { width:100%; }
    .person-actions input { flex:1; }
}

/* ---------- Lead call-to-action ----------
   The link to the lead list used to be a small yellow chip in a row of other
   small yellow chips, which is a poor showing for the one thing on the page
   with a customer waiting at the other end of it. It gets the full width, the
   count in the headline, and an arrow that moves on hover so it reads as
   somewhere to go rather than something to note. */
.lead-cta { display:flex; align-items:center; gap:16px; padding:15px 18px; margin-bottom:16px;
            border-radius:var(--radius); text-decoration:none; color:#fff;
            background:linear-gradient(102deg,var(--blue-800) 0%,var(--blue-600) 100%);
            box-shadow:0 2px 6px rgba(0,42,94,.18), 0 10px 26px rgba(0,42,94,.16);
            border:1px solid rgba(255,255,255,.1);
            transition:transform .18s var(--ease), box-shadow .18s var(--ease); }
.lead-cta:hover { transform:translateY(-2px);
                  box-shadow:0 4px 10px rgba(0,42,94,.22), 0 18px 40px rgba(0,42,94,.24); }
.lead-cta:focus-visible { outline:2px solid var(--yellow-500); outline-offset:3px; }
.lead-cta-icon { width:44px; height:44px; border-radius:13px; flex:none; display:grid;
                 place-items:center; background:rgba(255,255,255,.16); color:#fff; }
.lead-cta-body { min-width:0; flex:1; }
.lead-cta-title { font-size:16.5px; font-weight:680; letter-spacing:-.2px;
                  display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
.lead-cta-new { background:var(--yellow-500); color:var(--blue-900); font-size:11.5px;
                font-weight:700; padding:2px 10px; border-radius:999px; letter-spacing:.2px; }
.lead-cta-sub { font-size:12.5px; opacity:.82; margin-top:3px; }
.lead-cta-go { margin-left:auto; flex:none; background:#fff; color:var(--blue-800);
               font-weight:650; font-size:13.5px; padding:9px 16px; border-radius:9px;
               display:inline-flex; align-items:center; gap:8px; white-space:nowrap; }
.lead-cta-go svg { transition:transform .18s var(--ease); }
.lead-cta:hover .lead-cta-go svg { transform:translateX(3px); }
@media (max-width:720px) {
    .lead-cta { flex-wrap:wrap; gap:12px; }
    .lead-cta-go { margin-left:0; width:100%; justify-content:center; }
}

/* ---------- Cards ---------- */
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:16px 18px; margin-bottom:13px; box-shadow:var(--shadow); transition:.15s; }
.card { transition:box-shadow .18s var(--ease), border-color .18s var(--ease); }
.card:hover { box-shadow:var(--shadow-lg); border-color:#D3DEEC; }
.card-header { display:flex; justify-content:space-between; align-items:flex-start; gap:10px;
               margin-bottom:9px; flex-wrap:wrap; }
.card-header-left { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
.title { font-weight:650; font-size:14.5px; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
         box-shadow:0 1px 2px rgba(16,26,43,.04);
         padding:16px 18px; margin-bottom:16px; box-shadow:var(--shadow); }

/* ---------- Module cards (overview) ---------- */
.modgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); gap:14px; }
.modcard { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
           padding:18px; box-shadow:var(--shadow); display:flex; flex-direction:column;
           text-decoration:none; color:inherit; transition:.16s; border-top:3px solid var(--blue-600); }
.modcard { transition:transform .2s var(--ease), box-shadow .2s var(--ease); }
.modcard:hover { transform:translateY(-3px); box-shadow:var(--shadow-lg); }
/* The icon sits above the heading rather than beside it: the cards are the
   first thing on the overview, and a glyph is read before a word. */
.modcard-icon { width:40px; height:40px; border-radius:11px; display:grid; place-items:center;
                background:var(--blue-50); color:var(--blue-600); margin-bottom:12px; }
.modcard.planned .modcard-icon { background:#EFF2F7; color:var(--muted); }
.modcard.planned { border-top-color:var(--line); opacity:.72; }
.modcard.planned:hover { transform:none; }
.modcard h3 { margin:0 0 6px; font-size:15.5px; }
.modcard p { margin:0 0 14px; font-size:13px; color:var(--muted); flex:1; }
.modstat { display:flex; gap:16px; padding-top:12px; border-top:1px solid var(--line); }
.modstat div { font-size:12px; color:var(--muted); }
.modstat b { display:block; font-size:17px; color:var(--ink); font-weight:700; }

/* ---------- Product images ----------
   Sits between the draft copy and the approve buttons, because that is the
   order the reviewer works in: read the words, check the photos, decide. */
.img-strip { border:1px solid var(--line); border-radius:var(--radius); padding:11px 13px;
             margin:11px 0 0; background:var(--blue-50); }
.img-strip-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:9px; }
.img-strip-title { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.6px;
                   color:var(--blue-700); display:flex; align-items:center; }
.img-tiles { display:flex; gap:10px; flex-wrap:wrap; align-items:flex-start; }
.img-tile { margin:0; width:150px; background:var(--card); border:1px solid var(--line);
            border-radius:9px; overflow:hidden; box-shadow:var(--shadow); }
.img-tile img { display:block; width:100%; height:108px; object-fit:cover; background:#F7F9FC; }
.img-tile figcaption { padding:6px 8px 8px; }
.img-name { font-size:11.5px; font-weight:600; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; }
.img-meta { font-size:10.5px; color:var(--muted); margin-top:2px; display:flex; gap:4px;
            align-items:center; flex-wrap:wrap; }
.img-detail { font-size:10.5px; color:var(--amber); margin-top:4px; line-height:1.35; }
.img-tag { font-size:9.5px; font-weight:700; padding:1px 6px; border-radius:999px;
           text-transform:uppercase; letter-spacing:.3px; }
.img-tag.main { background:var(--yellow-500); color:var(--blue-800); }
.img-tag.ok { background:var(--green-bg); color:var(--green); }
.img-tag.pending { background:var(--amber-bg); color:var(--amber); }
.img-controls { display:flex; gap:4px; margin-top:6px; }
.img-btn { background:#fff; border:1px solid var(--line); color:var(--blue-700); font-size:10.5px;
           padding:3px 7px; border-radius:6px; font-weight:600; white-space:nowrap; }
.img-btn:hover { border-color:var(--blue-600); background:var(--blue-50); }
.img-btn.danger { color:var(--red); }
.img-btn.danger:hover { border-color:var(--red); background:var(--red-bg); }
/* The add tile matches a photo tile's footprint so the row does not reflow
   as images are added. */
.img-add { width:150px; min-height:150px; border:2px dashed var(--blue-100); border-radius:9px;
           display:flex; flex-direction:column; align-items:center; justify-content:center;
           gap:4px; cursor:pointer; color:var(--blue-700); background:var(--card);
           text-align:center; padding:8px; transition:.15s; }
.img-add:hover { border-color:var(--blue-600); background:var(--blue-50); }
.img-add.dragover { border-color:var(--yellow-500); background:var(--yellow-100); }
.img-add.full { cursor:default; border-style:solid; color:var(--muted); }
.img-add.full:hover { border-color:var(--blue-100); background:var(--card); }
.img-add-plus { font-size:21px; font-weight:700; line-height:1; }
.img-add-text { font-size:11.5px; font-weight:600; }
.img-add-text small { font-weight:400; color:var(--muted); }
.img-tile.busy { opacity:.5; }
.img-error { margin-top:9px; background:var(--red-bg); border:1px solid #F5C6C6;
             border-left:3px solid var(--red); border-radius:8px; padding:8px 11px;
             font-size:12px; color:#8E1F1F; }

/* ---------- Badges ---------- */
.badge { display:inline-block; font-size:10.5px; padding:2.5px 9px; border-radius:999px;
         font-weight:600; letter-spacing:.2px; }
.badge.small_model { background:var(--blue-100); color:var(--blue-700); }
.badge.fallback_model { background:#E7F3EC; color:#166534; }
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
/* Lead inbox. Type is the axis that matters most -- whether a CRM hand-off was
   even attempted -- so it gets colour rather than another grey pill. */
.badge.form_submission { background:var(--amber-bg); color:var(--amber); }
.badge.contact_shared { background:var(--green-bg); color:var(--green); }
.badge.intent_only { background:#EEF2F7; color:var(--muted); }
.badge.status-new { background:var(--blue-100); color:var(--blue-700); }
.badge.status-contacted { background:var(--green-bg); color:var(--green); }
.badge.status-won { background:var(--green); color:#fff; }
.badge.status-lost, .badge.status-ignored { background:#EEF2F7; color:var(--muted); }
.lead-contact { font-size:13px; }
.lead-contact a { text-decoration:none; }
.lead-none { color:var(--amber); font-size:12px; }
.logview { background:#0F172A; color:#E2E8F0; border-radius:8px; padding:14px 16px;
           font-family:Consolas,'Courier New',monospace; font-size:12px; line-height:1.5;
           max-height:560px; overflow:auto; white-space:pre; margin:0; }

/* Chat transcript drill-down. Two columns of bubbles so who said what is
   readable at a glance without any per-message labels. */
.transcript { display:flex; flex-direction:column; gap:10px; }
.chat-msg { max-width:78%; padding:10px 13px; border-radius:12px; font-size:13px;
            line-height:1.55; white-space:pre-wrap; overflow-wrap:anywhere; }
.chat-msg.user { align-self:flex-end; background:var(--blue-100); color:var(--blue-700);
                 border-bottom-right-radius:3px; }
.chat-msg.assistant { align-self:flex-start; background:#F4F5F7; color:var(--ink);
                      border-bottom-left-radius:3px; }
.chat-msg .who { display:block; font-size:10.5px; text-transform:uppercase;
                 letter-spacing:.04em; opacity:.7; margin-bottom:3px; }
.chat-msg.flagged { box-shadow:0 0 0 2px var(--red); }
.empty { color:var(--muted); text-align:center; padding:48px 20px; background:var(--card);
         border:1px dashed var(--line); border-radius:var(--radius); }
.empty-title { font-weight:600; color:var(--ink); margin-bottom:5px; font-size:14px; }

/* ---------- Toast ---------- */
@keyframes toast-in { from { opacity:0; transform:translateY(14px) scale(.97); } }
.toast { animation:toast-in .26s var(--ease); box-shadow:var(--shadow-lg);
         position:fixed; right:22px; bottom:22px; z-index:200; max-width:400px;
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
.btn-ghost:hover { border-color:var(--blue-600); background:var(--blue-50); }
button { transition:filter .16s var(--ease), box-shadow .16s var(--ease),
                    transform .12s var(--ease), background .16s var(--ease),
                    border-color .16s var(--ease); }
button:not(:disabled):hover { filter:brightness(1.06); }
button:not(:disabled):active { transform:translateY(1px); }
button:disabled { opacity:.5; cursor:not-allowed; }
.approve:hover, .reject:hover, .publish:hover, .btn-primary:hover, .btn-accent:hover {
    box-shadow:0 4px 14px rgba(16,26,43,.18); }
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

/* ---------- Custom draft sections ----------
   A reviewer-authored heading plus its bullet points, for products where
   Features and Applications are not the right two headings. */
.sections { margin-bottom:4px; }
.section-row { border:1px solid var(--yellow-300); background:#FFFDF5; border-radius:8px;
               padding:9px 11px; margin-bottom:8px; }
.section-row-head { display:flex; gap:8px; align-items:center; margin-bottom:6px; }
.section-heading { flex:1; font-size:13px; font-weight:600; padding:6px 9px;
                   border:1px solid var(--yellow-300); border-radius:6px; background:#fff; }
.section-items { margin-bottom:0; }

/* ---------- Regenerate model picker ---------- */
.model-pick { font-size:12.5px; padding:6px 9px; border:1px solid var(--line);
              border-radius:8px; background:#fff; color:var(--ink); max-width:220px; }
.model-pick:disabled { opacity:.6; }

/* ---------- Bars ---------- */
.filter-bar { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
              padding:12px 16px; margin-bottom:15px; display:flex; gap:14px; flex-wrap:wrap;
              align-items:center; box-shadow:var(--shadow); }
.filter-bar label { display:flex; align-items:center; gap:6px; }
.filter-bar a.clear { color:var(--muted); font-size:12.5px; }
.fetch-bar { background:linear-gradient(102deg,var(--blue-50),#fff 62%); border:1px solid var(--blue-100);
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

/* ---------- Login ----------
   The logo carries the branding on its own here. The strapline that used to
   sit beside it said what the company sells, which is not what this page is
   for -- the person signing in already works here. */
.login-bg { min-height:100vh; display:grid; place-items:center; padding:24px; position:relative;
            overflow:hidden;
            background:linear-gradient(150deg,var(--blue-900) 0%,var(--blue-800) 45%,var(--blue-600) 100%); }
/* Two soft washes of light, well outside the card, so the flat blue reads as
   depth rather than a fill. */
.login-bg::before, .login-bg::after { content:""; position:absolute; border-radius:50%;
                                       pointer-events:none; }
.login-bg::before { width:620px; height:620px; top:-260px; right:-180px;
                    background:radial-gradient(circle,rgba(255,199,44,.20),transparent 68%); }
.login-bg::after { width:720px; height:720px; bottom:-320px; left:-240px;
                   background:radial-gradient(circle,rgba(30,122,224,.34),transparent 68%); }
@keyframes login-in { from { opacity:0; transform:translateY(12px); } }
.login-card { background:#fff; border-radius:16px; padding:38px 34px 34px; width:100%; max-width:392px;
              box-shadow:0 24px 70px rgba(0,20,52,.4); position:relative; z-index:1;
              animation:login-in .3s var(--ease); }
.login-brand { display:flex; justify-content:center; margin-bottom:22px; }
.login-card h1 { font-size:21px; margin:0; text-align:center; letter-spacing:-.35px; }
.login-card .subtitle { margin:7px 0 24px; font-size:13px; text-align:center; max-width:none; }
.field { margin-bottom:15px; }
.field label { display:block; margin-bottom:5px; font-weight:600; color:var(--ink); font-size:12.5px; }
.field input { width:100%; padding:11px 13px; font-size:14px; border-radius:9px;
               border:1px solid var(--line); transition:border-color .16s, box-shadow .16s; }
.field input:focus { outline:none; border-color:var(--blue-500);
                     box-shadow:0 0 0 3px rgba(30,122,224,.16); }
.login-logo { display:inline-grid; place-items:center; padding:0; background:none; border:none; }
.login-logo img { display:block; height:52px; width:auto; }
.login-btn { width:100%; padding:12px; font-size:14px; font-weight:600; border-radius:9px;
             background:var(--blue-700); color:#fff; margin-top:6px; }
.login-err { background:var(--red-bg); border:1px solid #F5C6C6; color:#8E1F1F; padding:10px 12px;
             border-radius:9px; font-size:12.5px; margin-bottom:15px; }
.login-alt { text-align:center; margin-top:16px; font-size:12.5px; }
.login-alt a { color:var(--muted); text-decoration:none; }
.login-alt a:hover { color:var(--blue-700); text-decoration:underline; }
.login-rules { font-size:11.5px; color:var(--muted); margin:-8px 0 14px; }

/* ---------- Confirmation dialog ----------
   One dialog, used by every action on the dashboard. What it replaced was the
   browser's confirm(): it could only be attached to a form submit, so the
   fetch-driven buttons had none at all, it could not say what was about to
   happen beyond a single line, and it looked like a browser error. Actions
   here publish to Odoo, send customer-facing mail, delete staged files and
   disable logins -- and several sit right beside their opposite (Approve is
   one button along from Reject), so a misclick is easy to make and cannot be
   taken back from this side.
   Wired declaratively with data-confirm attributes; see SHARED_JS. */
.modal-back { position:fixed; inset:0; z-index:200; display:grid; place-items:center;
              padding:20px; background:rgba(15,32,60,.52); }
.modal-back[hidden] { display:none; }
.modal { background:var(--card); border-radius:12px; width:100%; max-width:440px;
         box-shadow:0 24px 60px rgba(15,32,60,.34); overflow:hidden;
         animation:modal-in .13s ease-out; }
@keyframes modal-in { from { opacity:0; transform:translateY(-7px) scale(.985); } }
@media (prefers-reduced-motion:reduce) { .modal { animation:none; } }
.modal-main { display:flex; gap:14px; padding:22px 22px 2px; }
.modal-icon { width:34px; height:34px; border-radius:50%; flex:none; display:grid;
              place-items:center; font-size:17px; font-weight:700; line-height:1;
              background:var(--blue-50); color:var(--blue-700); }
.modal-icon.danger { background:var(--red-bg); color:var(--red); }
.modal-icon.warn { background:var(--amber-bg); color:var(--amber); }
.modal-icon.go { background:var(--green-bg); color:var(--green); }
.modal-text { min-width:0; }
.modal h2 { margin:3px 0 6px; font-size:15.5px; line-height:1.35; }
.modal p { margin:0; font-size:13px; color:var(--muted); line-height:1.55; }
/* The subject of the action, quoted back. A dialog that says "Publish this
   description?" is only half an answer when six cards are open at once. */
.modal-what { margin:11px 0 0; padding:8px 11px; background:var(--bg);
              border:1px solid var(--line); border-left:3px solid var(--blue-600);
              border-radius:7px; font-size:12.5px; color:var(--ink);
              overflow-wrap:anywhere; }
.modal-foot { display:flex; justify-content:flex-end; gap:9px; padding:18px 22px 20px; }
.modal-foot button { padding:8px 17px; font-size:13px; }
@media (max-width:480px) {
    .modal-foot { flex-direction:column-reverse; }
    .modal-foot button { width:100%; }
}

@media (max-width:1180px) { :root { --sidebar:224px; } }

/* Below this the sidebar becomes a drawer: a permanent 224px column leaves too
   little for a review card, and these pages are read on a phone in the yard as
   well as at a desk. */
@media (max-width:980px) {
    .sidebar { transform:translateX(-100%); box-shadow:0 0 50px rgba(16,26,43,.35); }
    body.nav-open .sidebar { transform:none; }
    body.nav-open { overflow:hidden; }         /* the page behind must not scroll */
    .content { margin-left:0; }
    .burger { display:grid; }
    .topbar { padding:9px 18px; }
}
@media (max-width:720px) {
    /* Three columns in 375px turned every cell into one word per line. The
       table scrolls sideways instead: the first column stays put long enough
       to know which row you are reading, and nothing is truncated. */
    table.grid { display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; }
    table.grid > tbody { display:table; width:max-content; min-width:100%; }
    table.grid td, table.grid th { max-width:42ch; }

    .wrap { padding:18px 16px 56px; }
    .bulk-bar { position:static; }
    h1 { font-size:20px; }
    .page-head { gap:12px; }
    /* Long trails scroll sideways rather than stacking into three lines.
       Labels keep their full width (flex:none above) -- shrinking them to fit
       turned every step into two letters and an ellipsis. */
    .crumbs { flex-wrap:nowrap; overflow-x:auto;
              scrollbar-width:none; -webkit-overflow-scrolling:touch; }
    .crumbs::-webkit-scrollbar { display:none; }
    .crumb-current { max-width:24ch; }
}
"""

SHARED_JS = """
// ---------------------------------------------------------------------------
// The navigation drawer
//
// Only does anything on a narrow screen -- on a desktop the sidebar is always
// there and the control that calls this is hidden. Kept as one class on <body>
// so the panel, the backdrop and the page's own scroll lock all follow from a
// single state.
// ---------------------------------------------------------------------------
function setNav(open){
    document.body.classList.toggle('nav-open', open);
    const scrim = document.getElementById('sidebar-scrim');
    if(scrim) scrim.hidden = !open;
    const burger = document.getElementById('nav-toggle');
    if(burger) burger.setAttribute('aria-expanded', open ? 'true' : 'false');
}
function toggleNav(){ setNav(!document.body.classList.contains('nav-open')); }

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
// ---------------------------------------------------------------------------
// One confirmation dialog, for every action
//
// Everything on this dashboard that changes something asks first: publishing
// to Odoo, sending mail, regenerating drafts, deleting staged images, creating
// and disabling logins. None of it can be undone from here, and the buttons
// sit close together -- Approve is one button along from Reject.
//
// Wiring is declarative. Put data-confirm on a button and it is confirmed;
// there is nothing to remember when a new action is added, and no per-action
// handler to forget to write:
//
//   data-confirm        the question. "{n}" is the live count, "{s}" its plural
//   data-confirm-body   what will happen -- say the consequence, not "are you sure"
//   data-confirm-what   the subject, quoted back (a product title, a username)
//   data-confirm-ok     label on the go-ahead button (default "Confirm")
//   data-confirm-tone   danger | warn | go, or omitted for neutral blue
//   data-confirm-count  selector counted for {n}; no matches means no action
//
// confirmAction() is also callable directly, for the buttons that talk to the
// server with fetch instead of submitting a form.
// ---------------------------------------------------------------------------
function confirmAction(opts){
    const back = document.getElementById('confirm-modal');
    // Belt and braces: a page that somehow lacks the dialog markup must still
    // ask, rather than silently going ahead.
    if(!back) return Promise.resolve(window.confirm(opts.title || 'Are you sure?'));
    const icon = document.getElementById('confirm-icon');
    const text = document.getElementById('confirm-text');
    const what = document.getElementById('confirm-what');
    const yes  = document.getElementById('confirm-yes');
    const no   = document.getElementById('confirm-no');
    const tone = opts.tone || '';

    document.getElementById('confirm-title').textContent = opts.title || 'Are you sure?';
    text.textContent = opts.body || '';
    text.hidden = !opts.body;
    what.textContent = opts.what || '';
    what.hidden = !opts.what;
    icon.className = 'modal-icon ' + tone;
    icon.textContent = tone === 'go' ? '\u2713' : (tone === 'danger' || tone === 'warn') ? '!' : '?';
    yes.textContent = opts.ok || 'Confirm';
    yes.className = tone === 'danger' ? 'reject' : tone === 'go' ? 'approve' : 'btn-primary';

    const opener = document.activeElement;
    back.hidden = false;
    // Cancel takes the focus, not the go-ahead: a stray Enter or Space landing
    // on an unread dialog should not be what publishes to Odoo.
    no.focus();
    return new Promise(function(resolve){
        function close(answer){
            back.hidden = true;
            document.removeEventListener('keydown', onKey, true);
            yes.onclick = no.onclick = back.onmousedown = null;
            if(opener && opener.focus) opener.focus();
            resolve(answer);
        }
        function onKey(e){
            if(e.key === 'Escape'){ e.preventDefault(); close(false); }
            // Tab stays inside the dialog. Tabbing out of it lands on a page
            // you cannot see, which is how people confirm the wrong thing.
            if(e.key === 'Tab'){
                e.preventDefault();
                (document.activeElement === yes ? no : yes).focus();
            }
        }
        document.addEventListener('keydown', onKey, true);
        yes.onclick = function(){ close(true); };
        no.onclick  = function(){ close(false); };
        back.onmousedown = function(e){ if(e.target === back) close(false); };
    });
}

function confirmOptionsFrom(el){
    const d = el.dataset;
    const n = d.confirmCount ? document.querySelectorAll(d.confirmCount).length : null;
    function fill(v){
        // split/join rather than a regex: the braces would have to be escaped
        // for the regex and again for the Python string this lives in.
        return (v || '').split('{n}').join(n).split('{s}').join(n === 1 ? '' : 's');
    }
    return {title: fill(d.confirm), body: fill(d.confirmBody), what: d.confirmWhat,
            ok: fill(d.confirmOk), tone: d.confirmTone, count: n};
}

// Intercepted on click rather than on submit, because click is the only point
// at which the button that was pressed is known for certain. Several of these
// forms carry two buttons with different formactions -- guessing wrong there
// would approve something the reviewer meant to reject.
document.addEventListener('click', function(e){
    const el = e.target.closest ? e.target.closest('[data-confirm]') : null;
    if(!el || el.disabled) return;
    if(el.dataset.confirmDone === '1'){ delete el.dataset.confirmDone; return; }
    e.preventDefault();
    e.stopPropagation();
    const opts = confirmOptionsFrom(el);
    if(opts.count === 0) return;      // nothing ticked; nothing to ask about
    // Let the browser point at the empty required field before asking a
    // question whose answer it is going to throw away.
    const form = el.form;
    if(form && form.reportValidity && !form.reportValidity()) return;
    confirmAction(opts).then(function(ok){
        if(!ok) return;
        el.dataset.confirmDone = '1';
        el.click();                   // second time through, the guard above lets it past
    });
}, true);
function escapeHtml(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function toggleEdit(id){
    const w=document.querySelector(`.draft-content[data-row-id="${id}"]`);
    w.querySelector('.view-mode').style.display='none';
    w.querySelector('.edit-mode').style.display='block';
    const ta=w.querySelector('textarea[data-field="overview"]'); if(ta) ta.focus();
}
async function cancelEdit(id){
    const w=document.querySelector(`.draft-content[data-row-id="${id}"]`);
    // Only ask when there is something to lose. Opening the editor, changing
    // your mind and closing it again should not cost a dialog.
    const changed = [...w.querySelectorAll('textarea')].some(t => t.value !== t.defaultValue)
                     || w.querySelectorAll('.section-row').length > 0;
    if(changed){
        const ok = await confirmAction({
            title: 'Discard your edits?',
            body: 'The description goes back to what the model wrote. Anything you '
                  + 'typed here is lost.',
            ok: 'Discard', tone: 'danger'});
        if(!ok) return;
    }
    w.querySelectorAll('textarea').forEach(t=>t.value=t.defaultValue);
    // Sections added since the page loaded have no defaultValue to fall back
    // on -- they simply should not exist any more. Ones that were loaded with
    // the page are restored from their original attribute value.
    w.querySelectorAll('.section-row').forEach(row => {
        const heading=row.querySelector('.section-heading');
        if(heading && !heading.hasAttribute('value')) row.remove();
        else if(heading) heading.value = heading.getAttribute('value');
    });
    w.querySelector('.edit-mode').style.display='none';
    w.querySelector('.view-mode').style.display='block';
}
// ---------------------------------------------------------------------------
// Custom sections
//
// Features and Applications suit most products; some need a heading they do
// not -- Technical Details, Coverage, Care Instructions. Rather than adding a
// third fixed field (and then a fourth), the heading itself is editable.
//
// The inputs are named as parallel lists, so the browser submits however many
// there are with the approve form and the server zips them back together. No
// index bookkeeping to keep in step between the two.
// ---------------------------------------------------------------------------
const MAX_SECTIONS = 6;

function sectionRowHtml(){
    return `<div class="section-row">
      <div class="section-row-head">
        <input type="text" name="section_heading" class="section-heading" maxlength="60"
               placeholder="Section heading, e.g. Technical Details">
        <button type="button" class="img-btn danger" title="Remove this section"
                onclick="removeSection(this)">Remove</button>
      </div>
      <textarea name="section_items" class="edit-field section-items" rows="3"
                placeholder="One point per line"></textarea>
    </div>`;
}
function addSection(id){
    const wrap=document.querySelector(`[data-sections="${id}"]`);
    if(!wrap) return;
    if(wrap.querySelectorAll('.section-row').length >= MAX_SECTIONS){
        alert(`A product can have at most ${MAX_SECTIONS} extra sections.`);
        return;
    }
    wrap.insertAdjacentHTML('beforeend', sectionRowHtml());
    const added=wrap.lastElementChild.querySelector('.section-heading');
    if(added) added.focus();
}
async function removeSection(button){
    const row=button.closest('.section-row');
    if(!row) return;
    const heading=(row.querySelector('.section-heading')||{}).value || '';
    const items=(row.querySelector('.section-items')||{}).value || '';
    if(heading.trim() || items.trim()){
        const ok = await confirmAction({
            title: 'Remove this section?',
            body: 'The heading and its points are deleted from this draft.',
            what: heading.trim() || '(untitled section)', ok: 'Remove', tone: 'danger'});
        if(!ok) return;
    }
    row.remove();
}
function readSections(w){
    return [...w.querySelectorAll('.section-row')].map(row => ({
        heading: (row.querySelector('.section-heading')||{}).value?.trim() || '',
        items: ((row.querySelector('.section-items')||{}).value || '')
                 .split('\\n').map(s=>s.trim()).filter(Boolean),
    })).filter(s => s.heading && s.items.length);
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
    // Custom sections render after the standard two -- the same order the
    // assembler publishes them in, so the preview matches what goes to Odoo.
    for(const sec of readSections(w)){
        out+=`<div class="field-label">${escapeHtml(sec.heading)}</div><ul>`+
            sec.items.map(i=>`<li>${escapeHtml(i)}</li>`).join('')+`</ul>`;
    }
    w.querySelector('.editable-preview').innerHTML = out || '<span class="meta">(no content)</span>';
    w.querySelector('.edit-mode').style.display='none';
    w.querySelector('.view-mode').style.display='block';
    w.classList.add('locally-edited');
}

// ---------------------------------------------------------------------------
// Product images
//
// These talk to the server with fetch rather than posting a form. The image
// strip sits INSIDE the approve form -- a real submit here would either nest a
// form (invalid) or navigate away, throwing out whatever text edits the
// reviewer had in progress. So the strip re-renders itself in place instead.
// ---------------------------------------------------------------------------
const IMG_BASE = '/content-agent/queue';

function imgError(rowId, message){
    const box = document.querySelector(`[data-img-error="${rowId}"]`);
    if(!box) return;
    if(!message){ box.hidden = true; box.textContent = ''; return; }
    box.hidden = false;
    box.textContent = message;
}
function imgTile(rowId, img, max){
    const badges =
        (img.position === 0 ? '<span class="img-tag main">Main</span>' : '') +
        (img.verified ? '<span class="img-tag ok" title="Confirmed present on the product">On Odoo</span>'
         : img.published ? '<span class="img-tag pending" title="Sent to Odoo, not yet verified">Sent</span>' : '');
    const controls = img.published ? '' :
        `<div class="img-controls">` +
        (img.position === 0 ? '' :
          `<button type="button" class="img-btn" title="Use as the main product image"
                   onclick="imgPrimary(${rowId},${img.id},this.dataset.name)"
                   data-name="${escapeHtml(img.name)}">Set main</button>`) +
        `<button type="button" class="img-btn danger" title="Remove this image"
                 onclick="imgDelete(${rowId},${img.id},this.dataset.name)"
                 data-name="${escapeHtml(img.name)}">Remove</button></div>`;
    const detail = (img.detail && !img.verified)
        ? `<div class="img-detail">${escapeHtml(img.detail)}</div>` : '';
    return `<figure class="img-tile">
      <a href="${img.url}" target="_blank" rel="noopener" title="Open full size">
        <img src="${img.url}" alt="${escapeHtml(img.name)}" loading="lazy"></a>
      <figcaption>
        <div class="img-name" title="${escapeHtml(img.name)}">${escapeHtml(img.name)}</div>
        <div class="img-meta">${escapeHtml(img.size)}${badges}</div>
        ${detail}${controls}
      </figcaption></figure>`;
}
function imgRender(rowId, payload){
    const wrap = document.querySelector(`[data-img-tiles="${rowId}"]`);
    if(!wrap) return;
    const imgs = payload.images || [];
    const remaining = (payload.max || 0) - imgs.length;
    const adder = remaining > 0
        ? `<label class="img-add">
             <input type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple
                    onchange="imgUpload(${rowId}, this)" hidden>
             <span class="img-add-plus">+</span>
             <span class="img-add-text">Add photos<br><small>${remaining} left</small></span>
           </label>`
        : `<div class="img-add full"><span class="img-add-text">Limit of ${payload.max} reached</span></div>`;
    wrap.innerHTML = imgs.map(i => imgTile(rowId, i, payload.max)).join('') + adder;
    const count = document.querySelector(`[data-img-count="${rowId}"]`);
    if(count) count.textContent = imgs.length;
    imgWireDrop(rowId);
}
async function imgPost(rowId, url, options){
    imgError(rowId, '');
    const wrap = document.querySelector(`[data-img-tiles="${rowId}"]`);
    if(wrap) wrap.classList.add('busy');
    try {
        const res = await fetch(url, Object.assign({method:'POST'}, options||{}));
        const data = await res.json().catch(() => ({}));
        if(!res.ok){ imgError(rowId, data.error || 'That did not work. Try again.'); return null; }
        return data;
    } catch(e){
        imgError(rowId, 'Could not reach the server. Your text edits are still here -- try again.');
        return null;
    } finally {
        if(wrap) wrap.classList.remove('busy');
    }
}
async function imgUpload(rowId, input){
    const files = input.files;
    if(!files || !files.length) return;
    const body = new FormData();
    for(const f of files) body.append('files', f);
    input.value = '';   // so re-picking the same file still fires onchange
    const data = await imgPost(rowId, `${IMG_BASE}/${rowId}/images`, {body});
    if(!data) return;
    imgRender(rowId, data);
    if(data.errors && data.errors.length) imgError(rowId, data.errors.join(' '));
}
async function imgDelete(rowId, imageId, name){
    const ok = await confirmAction({
        title: 'Remove this photo?',
        body: 'The file is deleted from the server straight away. It is not sent to '
              + 'Odoo, and it would have to be uploaded again.',
        what: name || '', ok: 'Remove', tone: 'danger'});
    if(!ok) return;
    const data = await imgPost(rowId, `${IMG_BASE}/${rowId}/images/${imageId}/delete`);
    if(data) imgRender(rowId, data);
}
async function imgPrimary(rowId, imageId, name){
    const ok = await confirmAction({
        title: 'Use this as the main image?',
        body: 'It becomes the photo Odoo shows first for this product. The one '
              + 'currently in that spot moves into the gallery.',
        what: name || '', ok: 'Set as main', tone: 'go'});
    if(!ok) return;
    const data = await imgPost(rowId, `${IMG_BASE}/${rowId}/images/${imageId}/primary`);
    if(data) imgRender(rowId, data);
}
// Drag a photo straight onto the tile -- the common case is dragging off a
// desktop, and making that work costs three listeners.
function imgWireDrop(rowId){
    const label = document.querySelector(`[data-img-tiles="${rowId}"] .img-add`);
    if(!label || label.classList.contains('full')) return;
    const input = label.querySelector('input[type=file]');
    ['dragenter','dragover'].forEach(ev => label.addEventListener(ev, e => {
        e.preventDefault(); label.classList.add('dragover'); }));
    ['dragleave','drop'].forEach(ev => label.addEventListener(ev, e => {
        e.preventDefault(); label.classList.remove('dragover'); }));
    label.addEventListener('drop', e => {
        if(!e.dataTransfer || !e.dataTransfer.files.length || !input) return;
        input.files = e.dataTransfer.files;
        imgUpload(rowId, input);
    });
}

document.addEventListener('DOMContentLoaded', function(){
    const scrim = document.getElementById('sidebar-scrim');
    if(scrim) scrim.addEventListener('click', () => setNav(false));
    document.addEventListener('keydown', e => {
        if(e.key === 'Escape' && document.body.classList.contains('nav-open')) setNav(false);
    });
    // Following a link closes the drawer. Without this it stays open over the
    // page that was just asked for, which reads as the tap not having worked.
    document.querySelectorAll('.nav a').forEach(a =>
        a.addEventListener('click', () => setNav(false)));
    document.querySelectorAll('[data-img-tiles]').forEach(
        el => imgWireDrop(el.dataset.imgTiles));
    document.querySelectorAll('.row-check').forEach(cb => cb.addEventListener('change', updateSelCount));
    updateSelCount();
    // A breadcrumb too long for the viewport scrolls sideways. Start it at the
    // end: the page you are on is the useful part, parents are a swipe away.
    const cr=document.querySelector('.crumbs');
    if(cr && cr.scrollWidth > cr.clientWidth){ cr.scrollLeft = cr.scrollWidth; }
    const t=document.querySelector('.toast.ok');
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
    """Renders the one-shot message left by the action that redirected here.

    The message itself is stored server-side (see auth.put_flash); the browser
    only carries a random id in a cookie, so nothing about what happened --
    a name, an email address, an error from Odoo -- ever appears in the URL.
    """
    from dashboard import auth  # imported here: ui is loaded before auth is needed

    flash = auth.take_flash(request.cookies.get(auth.FLASH_COOKIE))
    if not flash:
        return ""
    kind, text = flash
    return (f'<div class="toast {kind}" role="status">{_esc(text)}'
            f'<button onclick="this.parentElement.remove()" title="Dismiss">&times;</button></div>')


HOME_ICON = ('<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">'
              '<path d="M8 1.7 1.4 7.2v.9h1.6v6.2h3.7v-3.8h2.6v3.8h3.7V8.1H15v-.9z"/></svg>')


def breadcrumb(trail: list[tuple[str, str]] | None) -> str:
    """Renders a navigation trail as (label, href) pairs.

    "Overview" is prepended for you, so a caller lists only the steps below the
    dashboard root. The final pair is the page you are on and renders as text
    rather than a link -- an href on it is ignored, so a caller can pass the
    page's own URL without it turning into a link back to itself.
    """
    if not trail:
        return ""
    steps = [("Overview", "/")] + [t for t in trail if t and t[0]]
    parts = []
    for i, (label, href) in enumerate(steps):
        icon = HOME_ICON if i == 0 else ""
        if i == len(steps) - 1 or not href:
            parts.append(f'<span class="crumb-current" aria-current="page">{icon}{_esc(label)}</span>')
        else:
            parts.append(f'<a href="{href}" title="{_esc(label)}">{icon}{_esc(label)}</a>')
    sep = '<span class="crumb-sep" aria-hidden="true">&rsaquo;</span>'
    return f'<nav class="crumbs" aria-label="Breadcrumb">{sep.join(parts)}</nav>'


BURGER_ICON = ('<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" '
                'stroke-width="1.9" stroke-linecap="round" aria-hidden="true">'
                '<path d="M4 7h16M4 12h16M4 17h16"/></svg>')


def _sidebar_html(active_module: str, user: dict | None, current_path: str = "") -> str:
    """The left-hand navigation, grouped and always visible on a desktop."""

    def link(item: dict, is_active: bool) -> str:
        current = ' aria-current="page"' if is_active else ""
        return (f'<a href="{item["path"]}" class="{"active" if is_active else ""}"{current}>'
                f'{icon(item.get("icon", ""))}<span>{_esc(item["label"])}</span></a>')

    links = "".join(link(i, i["key"] == active_module) for i in NAV_ITEMS)
    if user and user.get("role") == "admin":
        # Matched on the URL, not on active_module. These four share the
        # /settings prefix, so a single active_module=="settings" flag lit all
        # of them at once -- whichever one the current page sits under wins.
        links += '<div class="nav-group">Administration</div>'
        for item in ADMIN_NAV_ITEMS:
            links += link(item, path_matches(current_path, item["path"]))

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
                <button class="logout-btn" type="submit"
                        data-confirm="Sign out?"
                        data-confirm-body="Anything you have typed into a draft but not yet
 approved will be lost."
                        data-confirm-ok="Sign out">Sign out</button>
            </form>
        </div>"""

    return f"""
    <aside class="sidebar" id="sidebar">
      <a class="brand" href="/" aria-label="{_esc(APP_NAME)} -- home">
        <span class="brand-logo"><img src="{LOGO_URL}" alt="BC Sands"></span>
        <span class="brand-text">
          <span class="brand-title">{_esc(APP_NAME)}</span>
          <span class="brand-sub">{_esc(APP_SUBTITLE)}</span>
        </span>
      </a>
      <nav class="nav" aria-label="Sections">
        <div class="nav-group">Automations</div>
        {links}
      </nav>
      {user_block}
    </aside>"""


def page_shell(body: str, *, title: str = TITLE_SUFFIX,
                active_module: str = "", user: dict | None = None,
                request=None, fetch_running: bool = False,
                crumbs: list[tuple[str, str]] | None = None) -> str:
    toast = toast_html(request) if request is not None else ""
    current_path = request.url.path if request is not None else ""
    crumb_html = breadcrumb(crumbs)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="{BRAND_BLUE}">
<title>{_esc(title)}</title>
<link rel="icon" href="{LOGO_URL}">
<style>{THEME_CSS}</style>
<script>{SHARED_JS}</script>
</head>
<body data-fetch-running="{'1' if fetch_running else '0'}">
<a class="skip" href="#main">Skip to content</a>
<div class="shell">
{_sidebar_html(active_module, user, current_path)}
<div class="sidebar-scrim" id="sidebar-scrim" hidden></div>
<div class="content">
  <header class="topbar">
    <button class="burger" id="nav-toggle" type="button" onclick="toggleNav()"
            aria-label="Show navigation" aria-expanded="false"
            aria-controls="sidebar">{BURGER_ICON}</button>
    {crumb_html}
  </header>
  <main class="wrap" id="main">
{body}
  </main>
</div>
</div>
<!-- The confirmation dialog every action shares. Rendered once per page and
     filled in by confirmAction(); hidden until something asks. -->
<div class="modal-back" id="confirm-modal" hidden role="dialog" aria-modal="true"
     aria-labelledby="confirm-title" aria-describedby="confirm-text">
  <div class="modal">
    <div class="modal-main">
      <div class="modal-icon" id="confirm-icon" aria-hidden="true">?</div>
      <div class="modal-text">
        <h2 id="confirm-title"></h2>
        <p id="confirm-text"></p>
        <div class="modal-what" id="confirm-what" hidden></div>
      </div>
    </div>
    <div class="modal-foot">
      <button type="button" class="reopen" id="confirm-no">Cancel</button>
      <button type="button" class="btn-primary" id="confirm-yes">Confirm</button>
    </div>
  </div>
</div>
{toast}
</body>
</html>"""


def signed_out_page(*, heading: str, intro: str, form_html: str,
                     error: str = "", notice: str = "", footer_html: str = "") -> str:
    """The card the login page uses, for the other pages that appear before
    anyone has signed in: asking for a reset, and setting a new password.

    Shares the login page's shell so those pages look like part of the same
    system rather than something bolted on -- which matters more than usual
    here, because a password page that looks unfamiliar is one people
    reasonably refuse to type into.
    """
    err_html = f'<div class="login-err">{_esc(error)}</div>' if error else ""
    notice_html = (f'<div class="login-err" style="background:var(--blue-50);'
                    f'border-color:var(--blue-100);color:var(--blue-700)">{_esc(notice)}</div>'
                    ) if notice else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(heading)} &middot; {TITLE_SUFFIX}</title>
<link rel="icon" href="{LOGO_URL}">
<style>{THEME_CSS}</style>
</head>
<body>
<div class="login-bg">
  <div class="login-card">
    <div class="login-brand">
      <span class="login-logo"><img src="{LOGO_URL}" alt="BC Sands"></span>
    </div>
    <h1>{_esc(heading)}</h1>
    <p class="subtitle">{_esc(intro)}</p>
    {err_html}{notice_html}
    {form_html}
    {footer_html}
  </div>
</div>
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
<title>Sign in &middot; {TITLE_SUFFIX}</title>
<link rel="icon" href="{LOGO_URL}">
<style>{THEME_CSS}</style>
</head>
<body>
<div class="login-bg">
  <div class="login-card">
    <div class="login-brand">
      <span class="login-logo"><img src="{LOGO_URL}" alt="BC Sands"></span>
    </div>
    <h1>Content and Automation</h1>
    <p class="subtitle">Sign in to review product descriptions and chat insights.</p>
    {err_html}{notice_html}
    <form method="post" action="/login">
      <div class="field">
        <label for="username">Username or email</label>
        <input id="username" name="username" type="text" value="{_esc(username)}"
               autocomplete="username" autofocus required
               placeholder="jsmith or jane@bcsands.com.au">
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
      </div>
      <button class="login-btn" type="submit">Sign in</button>
    </form>
    <div class="login-alt"><a href="/forgot-password">Forgotten your password?</a></div>
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
