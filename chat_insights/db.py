"""
SQLite storage for chat insights.

Its own file (logs/chat_insights.db) rather than sharing review_queue.db, for
the reason dashboard/auth.py documents: rebuilding one store during work on the
other should never destroy it. content_seo_agent/db.py also can't host this --
its migration mechanism is column-only and every query hardcodes `review_queue`.

Same shape as the other two stores: a _connect() contextmanager that commits on
clean exit, and an idempotent init_db() called at the top of every public
function so there is no startup hook to forget.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "chat_insights.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT,
    week_end TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    conversation_count INTEGER DEFAULT 0,
    analysed_count INTEGER DEFAULT 0,
    truncated INTEGER DEFAULT 0,
    stats_json TEXT,
    report_html TEXT,
    email_status TEXT,
    email_detail TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    run_id INTEGER,
    created_at INTEGER,
    source TEXT,
    status TEXT,
    title TEXT,
    message_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    negative_feedback INTEGER DEFAULT 0,
    positive_feedback INTEGER DEFAULT 0,
    transcript TEXT,
    form_submission TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    conversation_id TEXT PRIMARY KEY,
    run_id INTEGER,
    topic TEXT,
    category TEXT,
    resolved INTEGER,
    is_lead INTEGER,
    sentiment TEXT,
    summary TEXT,
    lead_detail TEXT,
    bot_failed INTEGER DEFAULT 0,
    failure_reason TEXT,
    parse_ok INTEGER DEFAULT 1,
    analysed_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    conversation_id TEXT PRIMARY KEY,
    run_id INTEGER,
    created_at INTEGER,          -- when the conversation happened (epoch)
    detected_at TEXT,            -- when this row was written
    lead_type TEXT,              -- form_submission | contact_shared | intent_only
    category TEXT,               -- from the analysis: product_enquiry, stock_availability, ...
    topic TEXT,
    detail TEXT,
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    contact_source TEXT,         -- form | transcript | none
    status TEXT DEFAULT 'new',   -- new | contacted | won | lost | ignored
    owner TEXT,                  -- dashboard user who took it
    note TEXT,
    status_changed_at TEXT,
    crm_status TEXT DEFAULT 'unknown',  -- unknown | present | missing | manual
    crm_ref TEXT,
    crm_checked_at TEXT,
    alerted_at TEXT,             -- when a lead alert went out, so it goes out once
    handoff_state TEXT,          -- claimed_not_fired | no_claim | fired
    handoff_detail TEXT,         -- why we concluded that
    claim_excerpt TEXT,          -- what the customer was actually told
    digested_at TEXT             -- when the daily lead digest included it
);
CREATE TABLE IF NOT EXISTS lead_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_start TEXT,              -- first local day covered (YYYY-MM-DD)
    day_end TEXT,                -- last local day covered, inclusive
    day_label TEXT,              -- "yesterday", "Monday", "3 days"
    ran_at TEXT,                 -- when the run finished
    run_trigger TEXT,            -- scheduled | manual
    triggered_by TEXT,           -- dashboard username, for a manual run
    fetched INTEGER DEFAULT 0,   -- conversations pulled from Chatbase
    reported INTEGER DEFAULT 0,  -- leads in the reported window
    waiting INTEGER DEFAULT 0,   -- promised a callback the hand-off never made
    email_status TEXT,           -- see EMAIL_STATUSES
    email_detail TEXT,
    recipients TEXT,             -- who it actually went to
    subject TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_run ON conversations(run_id);
CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(run_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
"""

# Columns added after a table already existed in the field. Applied by init_db
# with ALTER TABLE, the same approach content_seo_agent/db.py uses, so an
# existing logs/chat_insights.db picks them up without being rebuilt -- these
# databases hold the only local copy of the transcripts.
_MIGRATION_COLUMNS = [
    ("conversations", "form_submission", "TEXT"),
    # Whether the bot's hand-off to the sales team actually ran. See
    # chat_insights/handoff.py -- claimed_not_fired is the one that costs money.
    ("leads", "handoff_state", "TEXT"),
    ("leads", "handoff_detail", "TEXT"),
    ("leads", "claim_excerpt", "TEXT"),
    # Separate from alerted_at: the per-lead alert and the daily digest are
    # different mailings, and one having gone out must not suppress the other.
    ("leads", "digested_at", "TEXT"),
    # When the model last refined this lead's details. Its presence is what
    # stops sync() paying for the same extraction on every run.
    ("leads", "extracted_at", "TEXT"),
]


LEAD_STATUSES = ("new", "contacted", "won", "lost", "ignored")
# A lead has to be contactable to be a lead. Buying intent with no phone and no
# email is still counted in the weekly report, but it is not something anyone
# can follow up, so it is not carried here.
LEAD_TYPES = ("form_submission", "contact_shared")

# How the hand-off to the sales team went. Mirrors chat_insights.handoff, kept
# here so queries and filters have one vocabulary to work from.
HANDOFF_STATES = ("claimed_not_fired", "no_claim", "fired")

# What happened to one morning's lead email. Deliberately more than sent/failed:
# three of these are the mailing working exactly as configured, and calling them
# failures would have Task Scheduler report a broken job most mornings.
EMAIL_SENT = "sent"                  # it went
EMAIL_FAILED = "failed"              # the relay refused it -- this is the bad one
EMAIL_NO_LEADS = "no_leads"          # a quiet day, and leads_email_when_empty is false
EMAIL_NO_RECIPIENTS = "no_recipients"  # nobody configured for daily_leads
EMAIL_NOT_REQUESTED = "not_requested"  # run with --no-email
EMAIL_STATUSES = (EMAIL_SENT, EMAIL_FAILED, EMAIL_NO_LEADS,
                   EMAIL_NO_RECIPIENTS, EMAIL_NOT_REQUESTED)

# Who started a run. The distinction matters: a morning covered only because
# somebody noticed and ran it by hand is not the same as one the overnight job
# handled, even though both end in an email.
TRIGGER_SCHEDULED = "scheduled"        # the overnight task
TRIGGER_MANUAL = "manual"              # the dashboard button
TRIGGER_COMMAND_LINE = "command line"  # a person at a terminal
TRIGGERS = (TRIGGER_SCHEDULED, TRIGGER_MANUAL, TRIGGER_COMMAND_LINE)

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Indexes over migrated columns. These cannot sit in SCHEMA, which runs before
# the ALTER TABLEs: on a database created before those columns existed, an index
# naming one would fail and take the whole init with it.
_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_leads_digested ON leads(digested_at);
CREATE INDEX IF NOT EXISTS idx_leads_handoff ON leads(handoff_state);
CREATE INDEX IF NOT EXISTS idx_lead_digests_day ON lead_digests(day_end);
"""


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, col_type in _MIGRATION_COLUMNS:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.executescript(_POST_MIGRATION_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def start_run(week_start: str, week_end: str) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO weekly_runs (week_start, week_end, started_at, status)
               VALUES (?, ?, ?, ?)""",
            (week_start, week_end, _now(), STATUS_RUNNING),
        )
        return cur.lastrowid


def finish_run(run_id: int, *, status: str, conversation_count: int = 0,
                analysed_count: int = 0, truncated: bool = False, stats: dict | None = None,
                report_html: str = "", email_status: str = "", email_detail: str = "",
                error: str = "") -> dict | None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """UPDATE weekly_runs
               SET finished_at = ?, status = ?, conversation_count = ?, analysed_count = ?,
                   truncated = ?, stats_json = ?, report_html = ?, email_status = ?,
                   email_detail = ?, error = ?
               WHERE id = ?""",
            (_now(), status, conversation_count, analysed_count, 1 if truncated else 0,
             json.dumps(stats or {}, ensure_ascii=False), report_html, email_status,
             email_detail, error, run_id),
        )
    return get_run(run_id)


def set_email_result(run_id: int, status: str, detail: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE weekly_runs SET email_status = ?, email_detail = ? WHERE id = ?",
                      (status, detail, run_id))


def get_run(run_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM weekly_runs WHERE id = ?", (run_id,)).fetchone()
        return _run_to_dict(row) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_run_to_dict(r) for r in rows]


def previous_complete_run(run_id: int) -> dict | None:
    """The completed run immediately before this one, for week-on-week deltas.

    Matched on week_start rather than id: runs can be re-run out of order, and
    "the week before this report's week" is the comparison a reader expects,
    not "whatever ran before it".
    """
    init_db()
    with _connect() as conn:
        current = conn.execute("SELECT week_start FROM weekly_runs WHERE id = ?",
                                (run_id,)).fetchone()
        if not current:
            return None
        row = conn.execute(
            """SELECT * FROM weekly_runs
               WHERE week_start < ? AND status = ? AND stats_json IS NOT NULL
               ORDER BY week_start DESC LIMIT 1""",
            (current["week_start"], STATUS_COMPLETE)).fetchone()
        return _run_to_dict(row) if row else None


def latest_run() -> dict | None:
    runs = list_runs(limit=1)
    return runs[0] if runs else None


def _run_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("stats_json"):
        try:
            d["stats"] = json.loads(d["stats_json"])
        except (json.JSONDecodeError, TypeError):
            d["stats"] = {}
    else:
        d["stats"] = {}
    return d


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def save_conversation(run_id: int | None, conv: dict) -> None:
    """Keyed on the Chatbase conversation id, so re-running a window updates in
    place rather than duplicating.

    run_id may be None: the daily lead job fetches conversations outside any
    weekly run, and those belong to no report.
    """
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO conversations
               (id, run_id, created_at, source, status, title, message_count,
                user_message_count, negative_feedback, positive_feedback, transcript,
                form_submission, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 -- Keep the run that already owns this conversation. The daily
                 -- lead job re-saves with run_id NULL, and overwriting would
                 -- orphan the row from its weekly report, breaking that
                 -- report's transcript drill-down.
                 run_id=COALESCE(excluded.run_id, conversations.run_id),
                 created_at=excluded.created_at,
                 source=excluded.source, status=excluded.status, title=excluded.title,
                 message_count=excluded.message_count,
                 user_message_count=excluded.user_message_count,
                 negative_feedback=excluded.negative_feedback,
                 positive_feedback=excluded.positive_feedback,
                 transcript=excluded.transcript,
                 -- Never let a re-fetch blank out contact details we already hold:
                 -- the form submission is the one part of a conversation that
                 -- cannot be reconstructed from anywhere else.
                 form_submission=COALESCE(excluded.form_submission, conversations.form_submission),
                 fetched_at=excluded.fetched_at""",
            (conv["id"], run_id, conv.get("created_at"), conv.get("source"), conv.get("status"),
             conv.get("title"), conv.get("message_count", 0), conv.get("user_message_count", 0),
             conv.get("negative_feedback", 0), conv.get("positive_feedback", 0),
             json.dumps(conv.get("messages") or [], ensure_ascii=False),
             json.dumps(conv["form_submission"], ensure_ascii=False)
             if conv.get("form_submission") else None,
             _now()),
        )


def get_conversation(conversation_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?",
                            (conversation_id,)).fetchone()
        return _conv_to_dict(row) if row else None


def list_conversations(run_id: int) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall()
        return [_conv_to_dict(r) for r in rows]


def _conv_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("form_submission"):
        try:
            d["form_submission"] = json.loads(d["form_submission"])
        except (json.JSONDecodeError, TypeError):
            d["form_submission"] = None
    if d.get("transcript"):
        try:
            d["messages"] = json.loads(d["transcript"])
        except (json.JSONDecodeError, TypeError):
            d["messages"] = []
    else:
        d["messages"] = []
    return d


def list_all_conversations(limit: int | None = None) -> list[dict]:
    """Every stored conversation, newest first, regardless of which run fetched
    it. Lead detection works from this rather than per-run lists: analyses are
    cached across runs, so a run-scoped query misses conversations analysed
    earlier."""
    init_db()
    with _connect() as conn:
        sql = "SELECT * FROM conversations ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_conv_to_dict(r) for r in conn.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# Leads
#
# A lead is stored once per conversation and updated in place. Detection fields
# (type, contacts, topic) are refreshed on every sync; anything a human touched
# -- status, owner, note, CRM state -- is never overwritten by a re-sync.
# ---------------------------------------------------------------------------

def upsert_lead(lead: dict) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO leads
               (conversation_id, run_id, created_at, detected_at, lead_type, category,
                topic, detail, contact_name, contact_email, contact_phone, contact_source,
                handoff_state, handoff_detail, claim_excerpt, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                 run_id=excluded.run_id, created_at=excluded.created_at,
                 lead_type=excluded.lead_type, category=excluded.category,
                 topic=excluded.topic, detail=excluded.detail,
                 -- Contact details only ever improve: a later sync that finds
                 -- nothing must not erase what an earlier one captured.
                 contact_name=COALESCE(excluded.contact_name, leads.contact_name),
                 contact_email=COALESCE(excluded.contact_email, leads.contact_email),
                 contact_phone=COALESCE(excluded.contact_phone, leads.contact_phone),
                 contact_source=excluded.contact_source,
                 handoff_state=excluded.handoff_state,
                 handoff_detail=excluded.handoff_detail,
                 claim_excerpt=excluded.claim_excerpt,
                 -- Never clear it: a later rules-only sync must not make the
                 -- lead look un-refined and buy the model call again.
                 extracted_at=COALESCE(excluded.extracted_at, leads.extracted_at)""",
            (lead["conversation_id"], lead.get("run_id"), lead.get("created_at"), _now(),
             lead.get("lead_type"), lead.get("category"), lead.get("topic"),
             lead.get("detail"), lead.get("contact_name"), lead.get("contact_email"),
             lead.get("contact_phone"), lead.get("contact_source"),
             lead.get("handoff_state"), lead.get("handoff_detail"),
             lead.get("claim_excerpt"),
             _now() if lead.get("extracted") else None),
        )


def list_leads(*, status: str = "", lead_type: str = "", category: str = "",
               since: int | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    init_db()
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if lead_type:
        where.append("lead_type = ?"); params.append(lead_type)
    if category:
        where.append("category = ?"); params.append(category)
    if since is not None:
        where.append("created_at >= ?"); params.append(since)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM leads{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def count_leads(*, status: str = "", lead_type: str = "", category: str = "",
                since: int | None = None) -> int:
    init_db()
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if lead_type:
        where.append("lead_type = ?"); params.append(lead_type)
    if category:
        where.append("category = ?"); params.append(category)
    if since is not None:
        where.append("created_at >= ?"); params.append(since)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM leads{clause}", params).fetchone()[0]


def lead_counts_by(field: str) -> dict:
    """Totals grouped by lead_type, category, status or handoff_state -- for the
    filter chips. Whitelisted because the field is interpolated into the SQL."""
    if field not in ("lead_type", "category", "status", "handoff_state"):
        raise ValueError(f"cannot group leads by {field!r}")
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {field} AS k, COUNT(*) AS n FROM leads GROUP BY {field} ORDER BY n DESC")
        return {(r["k"] or "uncategorised"): r["n"] for r in rows}


def get_lead(conversation_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE conversation_id = ?",
                            (conversation_id,)).fetchone()
        return dict(row) if row else None


def set_lead_status(conversation_id: str, status: str, *, owner: str = "",
                    note: str | None = None) -> None:
    if status not in LEAD_STATUSES:
        raise ValueError(f"unknown lead status {status!r}")
    init_db()
    with _connect() as conn:
        if note is None:
            conn.execute(
                """UPDATE leads SET status = ?, owner = ?, status_changed_at = ?
                   WHERE conversation_id = ?""",
                (status, owner, _now(), conversation_id))
        else:
            conn.execute(
                """UPDATE leads SET status = ?, owner = ?, note = ?, status_changed_at = ?
                   WHERE conversation_id = ?""",
                (status, owner, note, _now(), conversation_id))


def set_lead_crm_status(conversation_id: str, crm_status: str, *, ref: str = "") -> None:
    """Whether this lead is known to have reached the CRM. 'manual' records a
    human confirming it by eye, which is the only signal available while the
    hand-off runs through a Chatbase action into Zapier."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """UPDATE leads SET crm_status = ?, crm_ref = ?, crm_checked_at = ?
               WHERE conversation_id = ?""",
            (crm_status, ref, _now(), conversation_id))


def purge_contactless_leads() -> int:
    """Drops leads with no phone and no email. Returns how many went.

    Guarded on the row being untouched -- still `new`, nobody assigned, no note
    -- because the one thing worse than a useless row is deleting a lead
    somebody was working. A touched row is left alone regardless.
    """
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """DELETE FROM leads
               WHERE (contact_email IS NULL OR contact_email = '')
                 AND (contact_phone IS NULL OR contact_phone = '')
                 AND status = 'new'
                 AND (owner IS NULL OR owner = '')
                 AND (note IS NULL OR note = '')""")
        return cur.rowcount


def leads_awaiting_alert() -> list[dict]:
    """Leads that have never been alerted on, oldest first so a backlog goes out
    in the order the enquiries arrived."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE alerted_at IS NULL ORDER BY created_at ASC")
        return [dict(r) for r in rows]


def leads_in_window(start_epoch: int, end_epoch: int) -> list[dict]:
    """Leads from conversations that happened in [start, end).

    The daily digest reports a day, not a backlog: every lead from yesterday's
    chats, whether the hand-off went through or not. Ordered so the ones the
    sales team has to chase come first -- a customer who was told they would get
    a call is waiting on one, and nobody downstream knows to make it.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM leads
            WHERE created_at >= ? AND created_at < ?
            ORDER BY CASE handoff_state
                       WHEN 'claimed_not_fired' THEN 0
                       WHEN 'no_claim'          THEN 1
                       WHEN 'fired'             THEN 2
                       ELSE 3 END,
                     created_at ASC""", (int(start_epoch), int(end_epoch)))
        return [dict(r) for r in rows]


def leads_for_digest(*, include_digested: bool = False) -> list[dict]:
    """Every lead still open, most urgent first.

    Not what the daily email sends -- that reports a single day, see
    leads_in_window. This is the running backlog, used by the dashboard and
    available to a digest configured with a longer window.
    """
    init_db()
    clause = "" if include_digested else " AND status = 'new'"
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT * FROM leads
            WHERE 1=1{clause}
            ORDER BY CASE handoff_state
                       WHEN 'claimed_not_fired' THEN 0
                       WHEN 'no_claim'          THEN 1
                       WHEN 'fired'             THEN 2
                       ELSE 3 END,
                     created_at ASC""")
        return [dict(r) for r in rows]


def mark_leads_digested(conversation_ids: list[str]) -> None:
    """Records that these leads have been reported in a digest at least once."""
    if not conversation_ids:
        return
    init_db()
    now = _now()
    with _connect() as conn:
        conn.executemany("UPDATE leads SET digested_at = ? WHERE conversation_id = ?",
                          [(now, cid) for cid in conversation_ids])


def mark_lead_alerted(conversation_id: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE leads SET alerted_at = ? WHERE conversation_id = ?",
                      (_now(), conversation_id))


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The daily lead email
#
# Every morning's run is recorded here, whether it emailed anything or not.
# The point is not an audit trail for its own sake: this job is the one that
# catches customers who were told their details had been forwarded when they
# had not been, so a morning it silently did not run is a morning nobody knows
# is missing. A row per day makes the gap visible -- see missing_digest_days().
# ---------------------------------------------------------------------------

_DIGEST_FIELDS = ("day_start", "day_end", "day_label", "ran_at", "run_trigger",
                  "triggered_by", "fetched", "reported", "waiting",
                  "email_status", "email_detail", "recipients", "subject", "error")


def record_lead_digest(entry: dict) -> int:
    """Writes one morning's run. Returns the row id."""
    init_db()
    values = [entry.get(f) for f in _DIGEST_FIELDS]
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO lead_digests ({', '.join(_DIGEST_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(_DIGEST_FIELDS))})", values)
        return cur.lastrowid


def list_lead_digests(limit: int = 30) -> list[dict]:
    """Most recent runs first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lead_digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def latest_lead_digest() -> dict | None:
    rows = list_lead_digests(limit=1)
    return rows[0] if rows else None


def lead_digests_by_day(days: int = 14) -> dict[str, dict]:
    """The most recent run for each of the last `days` days it covered.

    Keyed on the day REPORTED, not the day it ran, so a manual catch-up run
    fills the gap it was meant to fill rather than looking like a second run
    for today.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lead_digests WHERE day_end IS NOT NULL "
            "ORDER BY day_end DESC, id DESC LIMIT ?", (days * 4,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["day_end"], dict(r))
    return out


def earliest_digest_day() -> str | None:
    """The oldest day any run has reported, or None if none ever has.

    Used to tell "the job did not run that morning" apart from "this system was
    not recording yet". Without it, switching the tracking on would announce a
    fortnight of failures that never happened -- which is the fastest way to
    teach people to ignore the warning.
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(day_end) AS first_day FROM lead_digests "
            "WHERE day_end IS NOT NULL AND day_end != ''").fetchone()
    return row["first_day"] if row and row["first_day"] else None


def save_analysis(run_id: int, conversation_id: str, analysis: dict) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO analyses
               (conversation_id, run_id, topic, category, resolved, is_lead, sentiment,
                summary, lead_detail, bot_failed, failure_reason, parse_ok, analysed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                 run_id=excluded.run_id, topic=excluded.topic, category=excluded.category,
                 resolved=excluded.resolved, is_lead=excluded.is_lead,
                 sentiment=excluded.sentiment, summary=excluded.summary,
                 lead_detail=excluded.lead_detail, bot_failed=excluded.bot_failed,
                 failure_reason=excluded.failure_reason, parse_ok=excluded.parse_ok,
                 analysed_at=excluded.analysed_at""",
            (conversation_id, run_id, analysis.get("topic"), analysis.get("category"),
             1 if analysis.get("resolved") else 0, 1 if analysis.get("is_lead") else 0,
             analysis.get("sentiment"), analysis.get("summary"), analysis.get("lead_detail"),
             1 if analysis.get("bot_failed") else 0, analysis.get("failure_reason"),
             1 if analysis.get("parse_ok", True) else 0, _now()),
        )


def get_analysis(conversation_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE conversation_id = ?",
                            (conversation_id,)).fetchone()
        return dict(row) if row else None


def list_analyses(run_id: int) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM analyses WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]
