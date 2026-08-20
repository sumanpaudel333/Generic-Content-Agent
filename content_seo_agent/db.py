"""
SQLite-backed review queue storage.

Replaces the earlier JSONL approach specifically because the dashboard
needs to UPDATE a row's status (pending -> approved/rejected), which
an append-only JSONL file can't do cleanly. SQLite needs no separate
server and no new dependency (sqlite3 is in the Python standard
library), which fits a single-server Windows deployment well.

If this ever needs to move to real Postgres (per the original roadmap,
once volume or concurrent access justifies it), only this file should
need to change -- pipeline.py and the dashboard call functions here,
not raw SQL.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

from content_seo_agent.constants import Status

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "review_queue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT,
    title TEXT,
    task_type TEXT,
    source TEXT,
    parsed_output TEXT,
    confidence TEXT,
    reasons TEXT,
    safety_flags TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    reviewed_at TEXT,
    reviewer_note TEXT,
    is_regulated INTEGER DEFAULT 0,
    ratio TEXT,
    quantity_detail TEXT,
    assembled_html TEXT,
    published INTEGER DEFAULT 0,
    published_at TEXT,
    odoo_write_detail TEXT
);
"""

# Columns added after initial release. Listed here so existing databases
# get migrated automatically via ALTER TABLE rather than needing a manual
# migration step or a fresh DB file.
_MIGRATION_COLUMNS = [
    ("is_regulated", "INTEGER DEFAULT 0"),
    ("ratio", "TEXT"),
    ("quantity_detail", "TEXT"),
    ("assembled_html", "TEXT"),
    ("published", "INTEGER DEFAULT 0"),
    ("published_at", "TEXT"),
    ("odoo_write_detail", "TEXT"),
    ("edited", "INTEGER DEFAULT 0"),
]


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
        conn.execute(SCHEMA)
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(review_queue)")}
        for col_name, col_type in _MIGRATION_COLUMNS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE review_queue ADD COLUMN {col_name} {col_type}")


def add_to_queue(
    product_id,
    title: str,
    task_type: str,
    source: str,
    parsed_output: dict | None,
    confidence: str,
    reasons: list[str],
    safety_flags: list[str],
    is_regulated: bool = False,
    ratio: str = "",
    quantity_detail: str = "",
) -> dict:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO review_queue
               (product_id, title, task_type, source, parsed_output, confidence,
                reasons, safety_flags, status, created_at, is_regulated, ratio, quantity_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(product_id), title, task_type, source,
                json.dumps(parsed_output, ensure_ascii=False) if parsed_output else None,
                confidence,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(safety_flags, ensure_ascii=False),
                Status.PENDING,
                now,
                1 if is_regulated else 0,
                ratio,
                quantity_detail,
            ),
        )
        row_id = cur.lastrowid

    return get_row(row_id)


def get_row(row_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (row_id,)).fetchone()
        return _row_to_dict(row) if row else None


def _build_filter_clause(
    status: str | None,
    task_type: str | None,
    source: str | None,
    confidence: str | None,
    has_safety_flags: bool | None,
    published: bool | None,
) -> tuple[str, list]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if confidence:
        clauses.append("confidence = ?")
        params.append(confidence)
    if has_safety_flags is not None:
        if has_safety_flags:
            clauses.append("safety_flags IS NOT NULL AND safety_flags != '[]'")
        else:
            clauses.append("(safety_flags IS NULL OR safety_flags = '[]')")
    if published is not None:
        clauses.append("published = ?")
        params.append(1 if published else 0)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def list_rows(
    status: str | None = None,
    task_type: str | None = None,
    source: str | None = None,
    confidence: str | None = None,
    has_safety_flags: bool | None = None,
    published: bool | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    init_db()
    where, params = _build_filter_clause(status, task_type, source, confidence, has_safety_flags, published)
    query = f"SELECT * FROM review_queue{where} ORDER BY created_at DESC"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_rows(
    status: str | None = None,
    task_type: str | None = None,
    source: str | None = None,
    confidence: str | None = None,
    has_safety_flags: bool | None = None,
    published: bool | None = None,
) -> int:
    init_db()
    where, params = _build_filter_clause(status, task_type, source, confidence, has_safety_flags, published)
    with _connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) as n FROM review_queue{where}", params).fetchone()
        return row["n"] if row else 0


def update_status(row_id: int, status: str, reviewer_note: str = "") -> dict | None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE review_queue SET status = ?, reviewed_at = ?, reviewer_note = ? WHERE id = ?",
            (status, now, reviewer_note, row_id),
        )
    return get_row(row_id)


def update_parsed_output(row_id: int, parsed_output: dict) -> dict | None:
    """Overwrites a row's draft/classification content -- used when a
    reviewer edits the text before approving, so what actually gets
    published is what they approved, not the model's original output.
    Marks the row `edited` so that provenance survives (e.g. for later
    deciding whether human-edited drafts should feed back into
    fine-tuning differently than as-generated ones)."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE review_queue SET parsed_output = ?, edited = 1 WHERE id = ?",
            (json.dumps(parsed_output, ensure_ascii=False), row_id),
        )
    return get_row(row_id)


def reopen_row(row_id: int) -> dict | None:
    """Moves a reviewed row back to pending, clearing the review decision so it
    reappears in the queue for another look. Does not touch publish state --
    a previously-published row that gets reopened, re-approved, and re-published
    will simply overwrite the earlier Odoo write."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE review_queue SET status = ?, reviewed_at = NULL, reviewer_note = '' WHERE id = ?",
            (Status.PENDING, row_id),
        )
    return get_row(row_id)


def publish_row(row_id: int, assembled_html: str, success: bool, detail: str) -> dict | None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE review_queue
               SET assembled_html = ?, published = ?, published_at = ?, odoo_write_detail = ?
               WHERE id = ?""",
            (assembled_html, 1 if success else 0, now, detail, row_id),
        )
    return get_row(row_id)


def counts_by_status() -> dict:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM review_queue GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("parsed_output", "reasons", "safety_flags"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
