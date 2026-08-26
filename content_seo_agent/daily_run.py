"""
The single job to schedule once every 24 hours (Windows Task Scheduler).

Deliberately NOT split into "backfill phase" vs "new product phase" --
those aren't actually different code paths. Every day this:

  1. Gets the current product list -- live from Odoo if configured,
     otherwise falls back to a static export file.
  2. Computes content_status for every product (deterministic, no
     model calls for this step).
  3. Skips anything already in the review queue (resume/dedup) or
     already "good".
  4. Processes up to DAILY_BATCH_SIZE of what's left.

On day one, most of what's left is the ~798-product backlog. Once
that's cleared, the same run just finds newly added products (if any)
-- there's no separate "mode" to switch. This also means a sudden bulk
import of new products gets naturally paced at DAILY_BATCH_SIZE/day
rather than flooding the review queue all at once.

Usage:
    python content_seo_agent\\daily_run.py

Schedule via Windows Task Scheduler to run once every 24 hours. See
README section "Scheduling" for setup steps.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from content_seo_agent import content_status as cs
from content_seo_agent.batch_runner import process_dataframe, load_products, SUMMARY_LOG_PATH
from connectors.odoo_connector import odoo_connector
from config import settings
from datetime import datetime, timezone

# config.yaml is the primary source; env vars override it if set (handy for
# a quick one-off change without editing the config file).
DAILY_BATCH_SIZE = int(os.environ.get("DAILY_BATCH_SIZE", settings.DAILY_BATCH_SIZE))
DELAY_SECONDS = float(os.environ.get("DAILY_RUN_DELAY", settings.DAILY_RUN_DELAY_SECONDS))
# Fallback source if your platform connector isn't configured for live pulls yet.
FALLBACK_EXPORT_PATH = os.environ.get("FALLBACK_PRODUCT_EXPORT", os.path.join("data", "Product-data.xlsx"))


def get_current_products() -> tuple[pd.DataFrame, str]:
    """Returns (dataframe, source_description).

    Deliberately checks settings.USE_ODOO_AS_PRODUCT_SOURCE, not just
    odoo_connector.is_configured() -- those are different questions.
    Odoo being "configured" only means write-back works once a draft is
    approved; it says nothing about whether Odoo is the right place to
    pull the CURRENT product list from right now. While working through
    an initial backlog import (e.g. from another system), Odoo can be
    fully configured for publish-testing while use_odoo_as_product_source
    stays false, so new-product fetching keeps using the backlog file
    until that's explicitly flipped."""
    if settings.USE_ODOO_AS_PRODUCT_SOURCE and odoo_connector.is_configured():
        try:
            print("Pulling live product list from Odoo...")
            products = odoo_connector.list_products()
            df = pd.DataFrame(products)
            print(f"Pulled {len(df)} products live from Odoo.")
            return df, "odoo_live"
        except Exception as e:
            print(f"Live Odoo pull failed ({e}). Falling back to static export file.")

    if not os.path.exists(FALLBACK_EXPORT_PATH):
        raise RuntimeError(
            f"Odoo not configured/reachable AND fallback file not found at "
            f"{FALLBACK_EXPORT_PATH}. Set ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY "
            f"in .env, or place a product export at that path."
        )
    print(f"Loading fallback export from {FALLBACK_EXPORT_PATH} ...")
    df = load_products(FALLBACK_EXPORT_PATH)
    return df, "static_file_fallback"


if __name__ == "__main__":
    run_started = datetime.now(timezone.utc).isoformat()
    print(f"=== Daily run started: {run_started} ===")
    print(f"Batch size: {DAILY_BATCH_SIZE}, delay: {DELAY_SECONDS}s")

    try:
        df, source = get_current_products()
    except Exception as e:
        print(f"FATAL: could not source a product list: {e}")
        sys.exit(1)

    results = process_dataframe(
        df,
        limit=DAILY_BATCH_SIZE,
        target_statuses=cs.NEEDS_DRAFTING,
        delay=DELAY_SECONDS,
        force=False,
    )

    summary = (
        f"\n{'='*60}\n"
        f"Daily run complete: {datetime.now(timezone.utc).isoformat()}\n"
        f"Source: {source}\n"
        f"Batch size limit: {DAILY_BATCH_SIZE}\n"
        f"Total attempted: {results['total']}\n"
        f"Processed successfully: {results['processed']}\n"
        f"Escalated past the fine-tuned model: {results['escalated']}\n"
        f"Flagged by safety filter: {results['safety_flagged']}\n"
        f"Failed: {results['failed']}\n"
        f"Elapsed: {results.get('elapsed_seconds', 0)/60:.1f} minutes\n"
        f"{'='*60}\n"
    )
    print(summary)
    os.makedirs(os.path.dirname(SUMMARY_LOG_PATH), exist_ok=True)
    with open(SUMMARY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(summary)
