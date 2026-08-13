"""
Review queue interface.

Thin wrapper over db.py, kept as a separate module so pipeline.py's
calling code (add_to_queue(...)) never needed to change when storage
moved from JSONL to SQLite. If storage changes again later (e.g. to
Postgres), this is the only file that should need to change.
"""
from content_seo_agent.db import (
    add_to_queue, get_row, list_rows, update_status, counts_by_status, publish_row,
)

__all__ = [
    "add_to_queue", "get_row", "list_rows", "update_status", "counts_by_status", "publish_row",
]
