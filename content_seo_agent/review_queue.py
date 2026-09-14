"""
Review queue interface.

Thin wrapper over db.py, kept as a separate module so pipeline.py's
calling code (add_to_queue(...)) never needed to change when storage
moved from JSONL to SQLite. If storage changes again later (e.g. to
Postgres), this is the only file that should need to change.
"""
from content_seo_agent.db import (
    add_to_queue, get_row, list_rows, count_rows, update_status, update_parsed_output, reopen_row,
    apply_regenerated_draft, counts_by_status, publish_row,
    record_audit, list_audit, count_audit, audit_actors, AUDIT_ACTIONS,
    add_image, list_images, list_images_for_rows, get_image, remove_image,
    set_primary_image, image_stats, images_awaiting_verification,
)

__all__ = [
    "add_to_queue", "get_row", "list_rows", "count_rows", "update_status", "update_parsed_output",
    "reopen_row", "apply_regenerated_draft", "counts_by_status", "publish_row",
    "record_audit", "list_audit", "count_audit", "audit_actors", "AUDIT_ACTIONS",
    "add_image", "list_images", "list_images_for_rows", "get_image", "remove_image",
    "set_primary_image", "image_stats", "images_awaiting_verification",
]
