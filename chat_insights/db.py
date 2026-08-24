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
CREATE INDEX IF NOT EXISTS idx_conversations_run ON conversations(run_id);
CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(run_id);
"""

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


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)


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

def save_conversation(run_id: int, conv: dict) -> None:
    """Keyed on the Chatbase conversation id, so re-running a window updates in
    place rather than duplicating."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO conversations
               (id, run_id, created_at, source, status, title, message_count,
                user_message_count, negative_feedback, positive_feedback, transcript, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 run_id=excluded.run_id, created_at=excluded.created_at,
                 source=excluded.source, status=excluded.status, title=excluded.title,
                 message_count=excluded.message_count,
                 user_message_count=excluded.user_message_count,
                 negative_feedback=excluded.negative_feedback,
                 positive_feedback=excluded.positive_feedback,
                 transcript=excluded.transcript, fetched_at=excluded.fetched_at""",
            (conv["id"], run_id, conv.get("created_at"), conv.get("source"), conv.get("status"),
             conv.get("title"), conv.get("message_count", 0), conv.get("user_message_count", 0),
             conv.get("negative_feedback", 0), conv.get("positive_feedback", 0),
             json.dumps(conv.get("messages") or [], ensure_ascii=False), _now()),
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
    if d.get("transcript"):
        try:
            d["messages"] = json.loads(d["transcript"])
        except (json.JSONDecodeError, TypeError):
            d["messages"] = []
    else:
        d["messages"] = []
    return d


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

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
