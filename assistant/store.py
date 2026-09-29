"""
SQLite storage for the staff assistant, in logs/assistant.db.

Its own file, like every other store here, so rebuilding one can never take
another with it. Holds two things:

    catalogue     a snapshot of Odoo's active products (name, SKU, whatever
                  description Odoo already has). 4,800 rows, refreshed on
                  demand -- reading them from Odoo takes about 8 seconds, far
                  too long to do while somebody waits for an answer.
    conversations what was asked, what came back, which products it was built
                  from, and whether the answer was any good.

The second is the point of the whole experiment: an assistant nobody can
check is not worth keeping, and "was it right?" has to be answerable from
data rather than from memory.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "assistant.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogue (
    sku TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,          -- whatever Odoo holds; often empty
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    actor TEXT,
    model TEXT,
    first_question TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    created_at TEXT,
    role TEXT,                 -- person | assistant
    text TEXT,
    model TEXT,
    ms INTEGER,
    sources_json TEXT,         -- the products the answer was built from
    flagged TEXT,              -- why the answer needs a second look, if it does
    rating TEXT,               -- good | bad
    rated_by TEXT,
    rated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
"""


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The product snapshot
# ---------------------------------------------------------------------------

def replace_catalogue(products: list[dict]) -> int:
    """Swaps in a fresh snapshot. Replaced whole rather than merged: a product
    removed in Odoo should stop being offered here, and a name that changed
    should change."""
    init_db()
    now = _now()
    rows = [(str(p.get("product_id") or "").strip(), (p.get("product_title") or "").strip(),
             (p.get("product_description") or "").strip(), now)
            for p in products if str(p.get("product_id") or "").strip()]
    with _connect() as conn:
        conn.execute("DELETE FROM catalogue")
        conn.executemany(
            "INSERT OR REPLACE INTO catalogue (sku, title, description, updated_at) "
            "VALUES (?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO meta (key, value) VALUES ('catalogue_refreshed_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
    return len(rows)


def catalogue_count() -> int:
    init_db()
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM catalogue").fetchone()["n"]


def catalogue_refreshed_at() -> str:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'catalogue_refreshed_at'").fetchone()
    return row["value"] if row else ""


def all_products() -> list[dict]:
    """Every product in the snapshot. Small enough (a few thousand rows) to
    score in memory per question, which keeps matching in Python where it can
    be read and tested rather than in SQL string matching."""
    init_db()
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT sku, title, description FROM catalogue")]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def start_conversation(actor: str, model: str, first_question: str) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (started_at, actor, model, first_question) "
            "VALUES (?, ?, ?, ?)", (_now(), actor, model, first_question[:300]))
        return cur.lastrowid


def add_message(conversation_id: int, role: str, text: str, *, model: str = "", ms: int = 0,
                sources: list[dict] | None = None, flagged: str = "") -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, created_at, role, text, model, ms, "
            "sources_json, flagged) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, _now(), role, text, model, int(ms),
             json.dumps(sources or []), flagged))
        return cur.lastrowid


def messages(conversation_id: int) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                            (conversation_id,)).fetchall()
    out = []
    for row in rows:
        message = dict(row)
        try:
            message["sources"] = json.loads(message.get("sources_json") or "[]")
        except ValueError:
            message["sources"] = []
        out.append(message)
    return out


def conversation(conversation_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?",
                           (conversation_id,)).fetchone()
    return dict(row) if row else None


def recent_conversations(limit: int = 15) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id "
            "AND m.role = 'person') AS questions FROM conversations c "
            "ORDER BY c.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def rate_message(message_id: int, rating: str, actor: str) -> bool:
    if rating not in ("good", "bad"):
        raise ValueError("rating must be good or bad")
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE messages SET rating = ?, rated_by = ?, rated_at = ? "
            "WHERE id = ? AND role = 'assistant'", (rating, actor, _now(), message_id))
        return cur.rowcount > 0


def conversation_of(message_id: int) -> int:
    """Which thread a message belongs to, so rating an answer returns to it."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT conversation_id FROM messages WHERE id = ?",
                           (message_id,)).fetchone()
    return row["conversation_id"] if row else 0


def ratings() -> dict:
    """How the answers have been judged so far -- the only honest measure of
    whether this is worth keeping."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS answers, "
            "SUM(CASE WHEN rating = 'good' THEN 1 ELSE 0 END) AS good, "
            "SUM(CASE WHEN rating = 'bad' THEN 1 ELSE 0 END) AS bad, "
            "SUM(CASE WHEN flagged != '' AND flagged IS NOT NULL THEN 1 ELSE 0 END) AS flagged "
            "FROM messages WHERE role = 'assistant'").fetchone()
    return {k: (row[k] or 0) for k in ("answers", "good", "bad", "flagged")}


def purge_conversation(conversation_id: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
