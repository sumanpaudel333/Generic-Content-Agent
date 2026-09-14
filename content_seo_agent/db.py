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
from contextlib import contextmanager
from datetime import datetime, timezone

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
    ("attempt_count", "INTEGER DEFAULT 1"),
    ("last_rejection_note", "TEXT"),
    # Who made the call. The dashboard has had real user accounts for a while,
    # but the decision was recorded without them -- rows carried a timestamp and
    # a note and no name, so "who approved this?" had no answer for content that
    # goes out to a live site.
    ("reviewed_by", "TEXT"),
]

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    actor TEXT,
    action TEXT,
    row_id INTEGER,
    product_id TEXT,
    title TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_row ON audit_log(row_id);
"""

# The vocabulary of auditable actions, so the filter list and the writers cannot
# drift apart. Publishing is included even though it is often automatic: what
# reached Odoo, and on whose approval, is the whole point of the trail.
AUDIT_ACTIONS = (
    "approved", "rejected", "edited", "published", "publish_failed",
    "reopened", "regenerated", "user_created", "user_disabled", "user_enabled",
    "password_reset", "images_added", "images_removed", "images_reclaimed",
)


# ---------------------------------------------------------------------------
# Reviewer-supplied product images
# ---------------------------------------------------------------------------
# Images are staged on this server between upload and publish, then pushed to
# Odoo in the same action that writes the description -- so a product goes live
# complete, instead of the copy landing now and the photos whenever somebody
# next opens Odoo.
#
# The row is the bookkeeping; the file itself lives on disk (see
# content_seo_agent/product_images.py). Three flags drive the lifecycle, and
# they are deliberately separate:
#
#   published  we asked Odoo to store it and Odoo did not complain
#   verified   we read the product back afterwards and the image was there
#   deleted_at the local staging copy has been reclaimed
#
# Only verified rows are ever eligible for cleanup. "Odoo returned success" is
# not the same claim as "the image is on the product", and the local copy is
# the only copy -- so the weaker claim is not allowed to delete anything.
IMAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id INTEGER NOT NULL,
    product_id TEXT,
    original_name TEXT,
    stored_name TEXT,
    content_type TEXT,
    byte_size INTEGER,
    checksum TEXT,
    position INTEGER DEFAULT 0,
    uploaded_at TEXT,
    uploaded_by TEXT,
    published INTEGER DEFAULT 0,
    published_at TEXT,
    odoo_image_id INTEGER,
    publish_detail TEXT,
    verified INTEGER DEFAULT 0,
    verified_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_img_row ON product_images(row_id);
CREATE INDEX IF NOT EXISTS idx_img_state ON product_images(published, verified, deleted_at);
-- Re-uploading the same file to the same product is a reviewer double-click,
-- not a second image. The checksum makes that a no-op rather than a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_img_row_checksum ON product_images(row_id, checksum);
"""


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
        conn.executescript(AUDIT_SCHEMA)
        conn.executescript(IMAGES_SCHEMA)
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


def update_status(row_id: int, status: str, reviewer_note: str = "",
                   actor: str = "") -> dict | None:
    """Records a review decision. `actor` is the signed-in user who made it.

    Optional so the CLI paths and tests keep working unchanged; the dashboard
    always passes one, and the audit trail is written separately by the caller
    that knows the wider context (a bulk action, an auto-publish).
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """UPDATE review_queue
               SET status = ?, reviewed_at = ?, reviewer_note = ?, reviewed_by = ?
               WHERE id = ?""",
            (status, now, reviewer_note, actor, row_id),
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
            """UPDATE review_queue
               SET status = ?, reviewed_at = NULL, reviewer_note = '', reviewed_by = NULL
               WHERE id = ?""",
            (Status.PENDING, row_id),
        )
    return get_row(row_id)


def apply_regenerated_draft(row_id: int, parsed_output: dict | None, source: str,
                             confidence: str, reasons: list[str], safety_flags: list[str]) -> dict | None:
    """Replaces a row's model output with a freshly generated draft and puts it
    back in the review queue.

    Used to give a rejected product another go rather than leaving it stranded:
    without this, a rejected product_id stays in the queue forever and is
    permanently skipped by batch_runner's dedup, so nothing would ever draft
    it again.

    The reviewer's rejection reason is carried into last_rejection_note so the
    next reviewer can see why the previous attempt was turned down, and
    attempt_count is incremented so repeated failures are visible rather than
    silently looping. The `edited` flag is cleared -- this is fresh model
    output, not something a human has touched."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT reviewer_note, attempt_count FROM review_queue WHERE id = ?",
                            (row_id,)).fetchone()
        if row is None:
            return None
        previous_note = row["reviewer_note"] or ""
        attempts = (row["attempt_count"] or 1) + 1
        conn.execute(
            """UPDATE review_queue
               SET parsed_output = ?, source = ?, confidence = ?, reasons = ?, safety_flags = ?,
                   status = ?, reviewed_at = NULL, reviewer_note = '',
                   last_rejection_note = ?, attempt_count = ?, edited = 0,
                   assembled_html = NULL, published = 0, published_at = NULL, odoo_write_detail = NULL
               WHERE id = ?""",
            (
                json.dumps(parsed_output, ensure_ascii=False) if parsed_output else None,
                source, confidence,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(safety_flags, ensure_ascii=False),
                Status.PENDING,
                previous_note, attempts, row_id,
            ),
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


# ---------------------------------------------------------------------------
# Audit trail
#
# Append-only by convention: there is no update or delete here, and nothing in
# the dashboard exposes one. A trail that can be edited from the thing it audits
# is not worth keeping.
# ---------------------------------------------------------------------------

def record_audit(actor: str, action: str, *, row_id: int | None = None,
                  product_id: str = "", title: str = "", detail: str = "") -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO audit_log (at, actor, action, row_id, product_id, title, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), actor or "unknown", action,
             row_id, str(product_id or ""), title[:300], detail[:1000]),
        )


def list_audit(*, actor: str = "", action: str = "", row_id: int | None = None,
                limit: int = 100, offset: int = 0) -> list[dict]:
    init_db()
    where, params = [], []
    if actor:
        where.append("actor = ?"); params.append(actor)
    if action:
        where.append("action = ?"); params.append(action)
    if row_id is not None:
        where.append("row_id = ?"); params.append(row_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def count_audit(*, actor: str = "", action: str = "", row_id: int | None = None) -> int:
    init_db()
    where, params = [], []
    if actor:
        where.append("actor = ?"); params.append(actor)
    if action:
        where.append("action = ?"); params.append(action)
    if row_id is not None:
        where.append("row_id = ?"); params.append(row_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM audit_log{clause}", params).fetchone()[0]


def audit_actors() -> list[str]:
    """Everyone who appears in the trail -- for the filter, including users who
    have since been removed from the dashboard."""
    init_db()
    with _connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT actor FROM audit_log ORDER BY actor")]


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


# ---------------------------------------------------------------------------
# Product images
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_image(row_id: int, product_id: str, *, original_name: str, stored_name: str,
               content_type: str, byte_size: int, checksum: str, position: int,
               uploaded_by: str) -> dict | None:
    """Records a staged image. Returns None if this exact file is already
    staged against this row -- see idx_img_row_checksum."""
    init_db()
    with _connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO product_images
                   (row_id, product_id, original_name, stored_name, content_type,
                    byte_size, checksum, position, uploaded_at, uploaded_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (row_id, str(product_id), original_name, stored_name, content_type,
                 byte_size, checksum, position, _now(), uploaded_by),
            )
        except sqlite3.IntegrityError:
            return None
        return _image_to_dict(
            conn.execute("SELECT * FROM product_images WHERE id=?", (cur.lastrowid,)).fetchone())


def list_images(row_id: int, *, include_deleted: bool = False) -> list[dict]:
    init_db()
    clause = "" if include_deleted else " AND deleted_at IS NULL"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM product_images WHERE row_id=?{clause} ORDER BY position, id",
            (row_id,)).fetchall()
        return [_image_to_dict(r) for r in rows]


def list_images_for_rows(row_ids: list[int]) -> dict[int, list[dict]]:
    """Images for a page of queue rows in one query.

    The review queue renders up to page_size cards at a time; asking per card
    turned one page into 25 round trips.
    """
    if not row_ids:
        return {}
    init_db()
    marks = ",".join("?" * len(row_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM product_images
                WHERE row_id IN ({marks}) AND deleted_at IS NULL
                ORDER BY position, id""", tuple(row_ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["row_id"], []).append(_image_to_dict(r))
    return out


def get_image(image_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        r = conn.execute("SELECT * FROM product_images WHERE id=?", (image_id,)).fetchone()
        return _image_to_dict(r) if r else None


def remove_image(image_id: int) -> dict | None:
    """Drops the record outright. Used when a reviewer removes an image before
    it was ever published -- there is nothing to keep a tombstone for. Images
    that DID reach Odoo are retired with mark_image_reclaimed instead, so the
    trail of what was sent survives the file being deleted."""
    img = get_image(image_id)
    if not img:
        return None
    with _connect() as conn:
        conn.execute("DELETE FROM product_images WHERE id=?", (image_id,))
    return img


def set_primary_image(row_id: int, image_id: int) -> None:
    """Position 0 is the product's main image; everything else is gallery.
    Renumbering the whole row keeps positions dense and unambiguous."""
    images = list_images(row_id)
    order = [i["id"] for i in images if i["id"] != image_id]
    if image_id in [i["id"] for i in images]:
        order.insert(0, image_id)
    with _connect() as conn:
        for pos, img_id in enumerate(order):
            conn.execute("UPDATE product_images SET position=? WHERE id=?", (pos, img_id))


def mark_image_published(image_id: int, *, success: bool, detail: str,
                          odoo_image_id: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE product_images
               SET published=?, published_at=?, odoo_image_id=?, publish_detail=?
               WHERE id=?""",
            (1 if success else 0, _now() if success else None, odoo_image_id, detail, image_id))


def mark_image_verified(image_id: int, *, ok: bool, detail: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE product_images SET verified=?, verified_at=?, publish_detail=?
               WHERE id=?""",
            (1 if ok else 0, _now() if ok else None,
             detail or None, image_id))


def mark_image_reclaimed(image_id: int) -> None:
    """The local staging file is gone; the record stays as history."""
    with _connect() as conn:
        conn.execute("UPDATE product_images SET deleted_at=? WHERE id=?", (_now(), image_id))


def images_awaiting_verification() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM product_images
               WHERE published=1 AND verified=0 AND deleted_at IS NULL
               ORDER BY published_at""").fetchall()
        return [_image_to_dict(r) for r in rows]


def images_reclaimable(before_iso: str) -> list[dict]:
    """Staged files safe to delete: on Odoo, seen on Odoo, and settled there
    long enough that a rollback would already have surfaced."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM product_images
               WHERE published=1 AND verified=1 AND deleted_at IS NULL
                 AND verified_at IS NOT NULL AND verified_at <= ?
               ORDER BY verified_at""", (before_iso,)).fetchall()
        return [_image_to_dict(r) for r in rows]


def image_stats() -> dict:
    """Counts and bytes per lifecycle state, for the storage admin page."""
    init_db()
    with _connect() as conn:
        def one(where: str) -> tuple[int, int]:
            r = conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(byte_size),0) b FROM product_images WHERE {where}"
            ).fetchone()
            return r["n"], r["b"]
        staged_n, staged_b = one("published=0 AND deleted_at IS NULL")
        unverified_n, unverified_b = one("published=1 AND verified=0 AND deleted_at IS NULL")
        verified_n, verified_b = one("published=1 AND verified=1 AND deleted_at IS NULL")
        reclaimed_n, _ = one("deleted_at IS NOT NULL")
        return {
            "staged": staged_n, "staged_bytes": staged_b,
            "unverified": unverified_n, "unverified_bytes": unverified_b,
            "verified": verified_n, "verified_bytes": verified_b,
            "reclaimed": reclaimed_n,
            "on_disk_bytes": staged_b + unverified_b + verified_b,
        }


def _image_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
