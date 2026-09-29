"""
Verifies published product images against the platform, then reclaims the
staged copies that are confirmed to have landed.

Run it from a scheduled task:

    python -m scripts.reclaim_images

Two steps, in this order, and the order is the point:

  1. Read each recently-published product back off Odoo and confirm the image
     is actually on it.
  2. Delete the local staging copy for images that passed step 1 and have been
     settled for product_images.retain_days_after_verify days.

An image that fails step 1 -- or a product that could not be read at all,
because Odoo was down or the credentials were wrong -- keeps its local copy.
This server holds the only copy of a staged image, so "the write returned
success" is never treated as sufficient reason to delete one.

Safe to run as often as you like: both steps are no-ops when there is nothing
in the relevant state.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from content_seo_agent import product_images, db  # noqa: E402  (after load_dotenv)
from connectors.odoo_connector import odoo_connector  # noqa: E402
from config import settings  # noqa: E402


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] reclaim_images starting")

    if not product_images.is_enabled():
        print("  product_images.enabled is false -- nothing to do.")
        return 0

    before = db.image_stats()
    print(f"  staged={before['staged']} sent-unverified={before['unverified']} "
          f"verified={before['verified']} "
          f"on disk={product_images.human_bytes(product_images.disk_usage())}")

    verify = product_images.verify_published(odoo_connector)
    print(f"  verify: checked={verify['checked']} confirmed={verify['verified']} "
          f"missing={verify['missing']} unreadable={verify['unknown']}")
    if verify["missing"]:
        print("  WARNING: images Odoo accepted are not on the product. Local copies kept; "
              "see Settings > Images in the dashboard.")

    result = product_images.reclaim()
    print(f"  reclaim: {result['removed']} file(s) removed, "
          f"{product_images.human_bytes(result['freed_bytes'])} freed "
          f"(retention {result['retain_days']} day(s))")
    if result["orphans"]:
        print(f"  swept {result['orphans']} orphaned file(s), "
              f"{product_images.human_bytes(result['orphan_bytes'])}")

    print(f"  on disk now: {product_images.human_bytes(product_images.disk_usage())}")
    print(f"[{stamp}] reclaim_images done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
