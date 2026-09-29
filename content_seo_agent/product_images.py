"""
Reviewer-supplied product images: staging, publishing, verification, cleanup.

Why this exists: the copy and the photos used to reach the product by two
different routes. This agent wrote the description to Odoo on approval, and
somebody opened Odoo later and attached the images by hand. Between those two
moments the product was live with text and no pictures, and "later" was
whenever anyone remembered.

So the reviewer now attaches the photos to the draft they are already looking
at, and both go out in the same action.

The lifecycle, and why the states are separate:

    staged      uploaded here, not sent anywhere yet
    published   Odoo accepted the write without complaining
    verified    we read the product back afterwards and the image was there
    reclaimed   the local staging copy has been deleted

published and verified are deliberately not the same flag. An XML-RPC call that
returns without raising is not proof that an image is on a product -- and while
a file is staged here, this server holds the only copy. So nothing is ever
deleted on the strength of the write alone: cleanup only ever touches images
that were read back off the product afterwards.

Validation is by magic bytes, not by the filename or the browser's declared
content type. Both of those are attacker-controlled, and this endpoint accepts
uploads: a file called photo.jpg that is not a JPEG is rejected here rather
than stored and served back to someone later.
"""
import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from content_seo_agent import db
from config import settings

logger = logging.getLogger("content_seo_agent.product_images")

# Staged files live outside the package, next to the database that indexes
# them. logs/ is already gitignored, so nobody's product photos land in a
# commit by accident.
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "product_images")

# Signature -> (content type, extension). Only formats Odoo's image fields
# handle, and only ones identifiable from their first bytes.
_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]

ACCEPT_ATTRIBUTE = "image/jpeg,image/png,image/gif,image/webp"


def is_enabled() -> bool:
    return bool(settings.IMAGES_ENABLED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sniff(data: bytes) -> tuple[str, str] | None:
    """Identifies an image from its leading bytes. Returns (content_type, ext)
    or None if this is not an image format we accept."""
    for signature, content_type, ext in _SIGNATURES:
        if data.startswith(signature):
            return content_type, ext
    # WebP is a RIFF container: "RIFF" <4-byte length> "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _row_dir(row_id: int) -> str:
    return os.path.join(IMAGE_DIR, str(row_id))


def path_for(image: dict) -> str:
    return os.path.join(_row_dir(image["row_id"]), image["stored_name"])


def exists_on_disk(image: dict) -> bool:
    return bool(image.get("stored_name")) and os.path.isfile(path_for(image))


def read_bytes(image: dict) -> bytes | None:
    try:
        with open(path_for(image), "rb") as fh:
            return fh.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def stage(row: dict, data: bytes, original_name: str, uploaded_by: str) -> tuple[dict | None, str]:
    """Validates and stores one uploaded file against a queue row.

    Returns (image, "") on success, or (None, reason) -- the reason is written
    for the reviewer to read, since they are the one who has to fix it.
    """
    if not is_enabled():
        return None, "Image upload is switched off in config.yaml (product_images.enabled)."
    if not data:
        return None, "That file was empty."
    if len(data) > settings.IMAGES_MAX_FILE_BYTES:
        return None, (f"{original_name} is {len(data) / 1024 / 1024:.1f} MB -- "
                       f"the limit is {settings.IMAGES_MAX_FILE_MB:g} MB.")

    kind = sniff(data)
    if not kind:
        return None, (f"{original_name} is not a JPEG, PNG, GIF or WebP image. "
                       f"(Checked the file's actual contents, not its name.)")
    content_type, ext = kind

    existing = db.list_images(row["id"])
    if len(existing) >= settings.IMAGES_MAX_PER_PRODUCT:
        return None, (f"This product already has {len(existing)} images -- "
                       f"the limit is {settings.IMAGES_MAX_PER_PRODUCT}.")

    checksum = hashlib.sha256(data).hexdigest()
    if any(i["checksum"] == checksum for i in existing):
        return None, f"{original_name} is already attached to this product."

    stored_name = f"{uuid.uuid4().hex}{ext}"
    directory = _row_dir(row["id"])
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, stored_name)
    try:
        with open(target, "wb") as fh:
            fh.write(data)
    except OSError as e:
        return None, f"Could not save {original_name}: {e}"

    image = db.add_image(
        row["id"], row["product_id"],
        original_name=os.path.basename(original_name or "image")[:180],
        stored_name=stored_name, content_type=content_type, byte_size=len(data),
        checksum=checksum, position=len(existing), uploaded_by=uploaded_by,
    )
    if image is None:
        # Lost a race against another upload of the same file. The record is
        # the source of truth, so the orphaned file goes rather than lingering.
        _unlink(target)
        return None, f"{original_name} is already attached to this product."
    return image, ""


def remove(image_id: int) -> tuple[bool, str]:
    """Reviewer removing an image before publish. Deletes file and record."""
    image = db.get_image(image_id)
    if not image:
        return False, "That image is no longer attached."
    if image.get("published"):
        return False, ("That image has already been published to Odoo -- remove it there, "
                        "not here, or the two will disagree.")
    _unlink(path_for(image))
    db.remove_image(image_id)
    _renumber(image["row_id"])
    return True, ""


def set_primary(row_id: int, image_id: int) -> None:
    db.set_primary_image(row_id, image_id)


def _renumber(row_id: int) -> None:
    """Keeps positions dense after a removal, so position 0 always exists and
    always means "the main product shot"."""
    images = db.list_images(row_id)
    if images:
        db.set_primary_image(row_id, images[0]["id"])


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
def publish_for_row(row: dict, connector) -> dict:
    """Sends this row's unpublished images to the platform.

    Called straight after the description write, so the product gains its copy
    and its photos in one move. Already-published images are skipped, which is
    what makes "Retry publish" safe to press twice.
    """
    summary = {"attempted": 0, "published": 0, "failed": 0, "detail": "", "skipped": False}
    if not is_enabled():
        summary["skipped"] = True
        return summary

    pending = [i for i in db.list_images(row["id"])
               if not i.get("published") and exists_on_disk(i)]
    if not pending:
        summary["skipped"] = True
        return summary

    if not connector.supports_images():
        summary["detail"] = "The configured connector cannot upload images."
        summary["failed"] = len(pending)
        return summary

    payload = []
    for image in pending:
        data = read_bytes(image)
        if data is None:
            db.mark_image_published(image["id"], success=False,
                                     detail="Staged file missing on disk at publish time.")
            summary["failed"] += 1
            continue
        payload.append({
            "ref": image["id"],
            "name": image["original_name"],
            "b64": base64.b64encode(data).decode("ascii"),
            "position": image["position"],
        })

    if not payload:
        summary["detail"] = "No staged files could be read."
        return summary

    summary["attempted"] = len(payload)
    result = connector.write_product_images(row["product_id"], payload)

    for entry in result.get("results", []):
        db.mark_image_published(
            entry["ref"], success=bool(entry.get("success")),
            detail=entry.get("detail", ""), odoo_image_id=entry.get("odoo_image_id"))
        if entry.get("success"):
            summary["published"] += 1
        else:
            summary["failed"] += 1

    if not result.get("results"):
        # The call failed before it got as far as individual images (not
        # configured, SKU not found). Record that against each one so the
        # reviewer sees why, rather than an image that is silently still staged.
        for item in payload:
            db.mark_image_published(item["ref"], success=False,
                                     detail=result.get("detail", "Image write failed."))
        summary["failed"] += len(payload)

    summary["detail"] = result.get("detail", "")
    return summary


# ---------------------------------------------------------------------------
# Verification and cleanup
# ---------------------------------------------------------------------------
def verify_published(connector) -> dict:
    """Reads products back and confirms their images are really there.

    This is the gate on deletion. Until an image passes through here it keeps
    its local copy, however cheerful the write result was.
    """
    result = {"checked": 0, "verified": 0, "missing": 0, "unknown": 0}
    awaiting = db.images_awaiting_verification()
    if not awaiting:
        return result
    if not connector.supports_images():
        result["unknown"] = len(awaiting)
        return result

    by_product: dict[str, list[dict]] = {}
    for image in awaiting:
        by_product.setdefault(str(image["product_id"]), []).append(image)

    for product_id, images in by_product.items():
        state = connector.get_product_image_state(product_id)
        if state is None:
            # Could not read the product -- says nothing about the images, so
            # they stay unverified and keep their files.
            result["unknown"] += len(images)
            continue
        for image in images:
            result["checked"] += 1
            if image["position"] == 0:
                present = bool(state.get("has_main"))
            else:
                present = image.get("odoo_image_id") in state.get("extra_ids", set())
            if present:
                db.mark_image_verified(image["id"], ok=True)
                result["verified"] += 1
            else:
                db.mark_image_verified(
                    image["id"], ok=False,
                    detail="Publish reported success but the image was not on the product "
                            "when read back. Local copy kept.")
                result["missing"] += 1
    return result


def reclaim(retain_days: int | None = None) -> dict:
    """Deletes staged files that are published, verified, and past the
    retention window. The database rows stay -- what was sent, by whom, and
    when is history worth keeping after the bytes are gone."""
    days = settings.IMAGES_RETAIN_DAYS if retain_days is None else max(0, int(retain_days))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    freed = 0
    removed = 0
    for image in db.images_reclaimable(cutoff):
        if exists_on_disk(image):
            freed += image["byte_size"] or 0
            _unlink(path_for(image))
        db.mark_image_reclaimed(image["id"])
        removed += 1
    orphans, orphan_bytes = sweep_orphans()
    return {"removed": removed, "freed_bytes": freed,
            "orphans": orphans, "orphan_bytes": orphan_bytes, "retain_days": days}


def sweep_orphans() -> tuple[int, int]:
    """Files on disk that no record points at -- left behind by a crash between
    writing the file and inserting its row. Harmless, but they are nobody's
    images and they never get cleaned up otherwise."""
    if not os.path.isdir(IMAGE_DIR):
        return 0, 0
    count = freed = 0
    for entry in os.scandir(IMAGE_DIR):
        if not entry.is_dir():
            continue
        try:
            row_id = int(entry.name)
        except ValueError:
            continue
        known = {i["stored_name"] for i in db.list_images(row_id, include_deleted=True)}
        for file_entry in os.scandir(entry.path):
            if file_entry.is_file() and file_entry.name not in known:
                try:
                    freed += file_entry.stat().st_size
                except OSError:
                    pass
                _unlink(file_entry.path)
                count += 1
        try:
            os.rmdir(entry.path)  # only succeeds once the directory is empty
        except OSError:
            pass
    return count, freed


def disk_usage() -> int:
    """Bytes actually on disk, which drifts from the sum of byte_size once
    files are reclaimed -- so the storage page reports the real number."""
    total = 0
    if not os.path.isdir(IMAGE_DIR):
        return 0
    for root, _dirs, files in os.walk(IMAGE_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_bytes(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
