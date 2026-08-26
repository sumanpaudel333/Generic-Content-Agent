"""
Batch runner: processes a product export through the Content/SEO Agent
pipeline, targeting products that actually need drafting.

Usage (from project root, C:\\BCSands\\agents):

    python -m content_seo_agent.batch_runner --input path\\to\\export.xlsx

Useful flags:
    --limit N Only process the first N matching products (good for a trial run)
    --statuses a,b,c Override which content_status values to target
                          (default: content_status.NEEDS_DRAFTING)
    --delay 1.5 Seconds to wait between products
                          (default: config.yaml's pipeline.daily_run_delay_seconds)
    --force Reprocess products even if already in the review queue

Resume behaviour: by default, any product_id already present in the
review queue (any status -- pending, approved, rejected) is skipped.
This means a run that gets interrupted (server restart, Ctrl+C, error)
can simply be re-run with the same command and it will pick up where
it left off, rather than reprocessing everything or requiring manual
tracking of what's done.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from content_seo_agent import pipeline, review_queue, content_status as cs
from content_seo_agent.constants import TaskType, Source
from content_seo_agent.small_model_client import is_available as small_model_available
from config import settings

SUMMARY_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "batch_run_summary.log")


def load_products(input_path: str) -> pd.DataFrame:
    if input_path.lower().endswith(".csv"):
        df = pd.read_csv(input_path)
    else:
        df = pd.read_excel(input_path)

    expected = ["product_id", "product_title", "product_description"]

    if set(expected).issubset(set(df.columns)):
        df = df[expected].copy()
    else:
        # Column names don't match. Check whether this looks like a
        # headerless export -- i.e. the "column names" pandas picked up
        # are actually the first data row (long text, HTML markup, or
        # a plain numeric ID rather than a short header word). If so,
        # re-read with no header and assign columns positionally,
        # otherwise the very first real product silently gets treated
        # as a header and dropped.
        looks_like_data = any(
            len(str(c)) > 40 or "<" in str(c) or str(c).strip().isdigit()
            for c in df.columns
        )
        if looks_like_data:
            print("NOTICE: this file has no header row -- the first product was being "
                  "read as column headers and would have been silently dropped. "
                  "Re-reading with positional column mapping "
                  "(column 1 = product_id, column 2 = product_title, column 3 = product_description).")
            if input_path.lower().endswith(".csv"):
                df = pd.read_csv(input_path, header=None)
            else:
                df = pd.read_excel(input_path, header=None)
            df = df.iloc[:, :3]
            df.columns = expected
        elif len(df.columns) >= 3:
            # Has a header row, just different names than expected.
            # Assume the same column ORDER (id, title, description) and
            # map positionally rather than failing outright.
            actual_names = df.columns[:3].tolist()
            print(f"NOTICE: expected columns {expected} but found {actual_names}. "
                  f"Mapping positionally (1st->product_id, 2nd->product_title, 3rd->product_description). "
                  f"If that's wrong for this file, the columns need reordering.")
            df = df.iloc[:, :3].copy()
            df.columns = expected
        else:
            raise ValueError(
                f"Could not find or infer the required columns {expected} in this file. "
                f"Found: {df.columns.tolist()}"
            )

    df["product_description"] = df["product_description"].fillna("")
    return df


def get_already_processed_ids() -> set:
    """Product IDs that already have a draft row, at ANY status.

    Rejected products are deliberately included here, so a scheduled run never
    silently re-drafts something a person turned down -- at best it would burn
    model calls reproducing the same rejected copy, at worst it would loop
    forever. Giving a rejected product another attempt is an explicit action
    instead: the dashboard's Rejected tab, which calls
    pipeline.regenerate_draft_for_row() with a higher temperature and the
    rejection reason as context."""
    rows = review_queue.list_rows(task_type=TaskType.DRAFT)
    return {str(r["product_id"]) for r in rows}


def process_dataframe(df: pd.DataFrame, limit: int | None, target_statuses: set, delay: float, force: bool) -> dict:
    """
    Core processing loop, shared by the manual CLI (run_batch) and the
    scheduled daily job (daily_run.py). Takes an already-loaded
    DataFrame with product_id/product_title/product_description
    columns -- caller decides whether that came from a static file or
    a live Odoo pull.

    Returns the results summary dict.
    """
    if not small_model_available():
        print("WARNING: Ollama is not reachable, so neither local rung of the escalation "
              "chain can run. Every product in this run escalates straight to Claude, "
              "which is slower and costs more per product. Check Ollama is running "
              "before continuing a large batch.")
    for label, ok, detail in pipeline.escalation_status():
        print(f"  [{'ok' if ok else '--'}] {label}: {detail}")

    df = df.copy()
    df["content_status"] = df.apply(
        lambda r: cs.determine_content_status(str(r["product_title"]), str(r["product_description"])),
        axis=1,
    )

    target_df = df[df["content_status"].isin(target_statuses)].copy()
    print(f"{len(target_df)} products match target status(es): {sorted(target_statuses)}")

    if not force:
        already_done = get_already_processed_ids()
        before = len(target_df)
        target_df = target_df[~target_df["product_id"].astype(str).isin(already_done)]
        skipped = before - len(target_df)
        if skipped:
            print(f"Skipping {skipped} products already in the review queue (resume behaviour). "
                  f"Use --force to reprocess them.")

    if limit is not None:
        target_df = target_df.head(limit)
        print(f"Limiting this run to {len(target_df)} products.")

    total = len(target_df)
    results = {"processed": 0, "escalated": 0, "safety_flagged": 0, "failed": 0, "total": total}
    if total == 0:
        print("Nothing to process.")
        return results

    print(f"\nProcessing {total} products, {delay}s delay between each...\n")
    start_time = time.time()

    for i, (_, row) in enumerate(target_df.iterrows(), start=1):
        product_id = row["product_id"]
        title = str(row["product_title"])
        print(f"[{i}/{total}] {title[:70]}")

        try:
            draft_row = pipeline.process_drafting(product_id, title)
            results["processed"] += 1
            if draft_row.get("source") != Source.SMALL_MODEL:
                results["escalated"] += 1
                results.setdefault("by_source", {})
                results["by_source"][draft_row["source"]] = \
                    results["by_source"].get(draft_row["source"], 0) + 1
            if draft_row.get("safety_flags"):
                results["safety_flagged"] += 1
                print(f"    -> flagged for review: {draft_row['safety_flags']}")
        except Exception as e:
            results["failed"] += 1
            print(f"    -> FAILED: {e}")

        if i < total:
            time.sleep(delay)

    results["elapsed_seconds"] = time.time() - start_time
    return results


def run_batch(input_path: str, limit: int | None, target_statuses: set, delay: float, force: bool):
    print(f"Loading products from {input_path} ...")
    df = load_products(input_path)
    print(f"Loaded {len(df)} total products.")

    results = process_dataframe(df, limit, target_statuses, delay, force)

    summary = (
        f"\n{'='*60}\n"
        f"Batch run complete: {datetime.now(timezone.utc).isoformat()}\n"
        f"Input: {input_path}\n"
        f"Target statuses: {sorted(target_statuses)}\n"
        f"Total attempted: {results['total']}\n"
        f"Processed successfully: {results['processed']}\n"
        f"Escalated past the fine-tuned model: {results['escalated']}"
        + (f" ({', '.join(f'{k}: {v}' for k, v in results.get('by_source', {}).items())})"
           if results.get("by_source") else "") + "\n"
        f"Flagged by safety filter: {results['safety_flagged']}\n"
        f"Failed: {results['failed']}\n"
        f"Elapsed: {results.get('elapsed_seconds', 0)/60:.1f} minutes\n"
        f"{'='*60}\n"
    )
    print(summary)

    os.makedirs(os.path.dirname(SUMMARY_LOG_PATH), exist_ok=True)
    with open(SUMMARY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-process products through the Content/SEO Agent.")
    parser.add_argument("--input", required=True, help="Path to product export (.xlsx or .csv)")
    parser.add_argument("--limit", type=int, default=None, help="Max products to process this run")
    parser.add_argument(
        "--statuses", default=",".join(sorted(cs.NEEDS_DRAFTING)),
        help="Comma-separated content_status values to target",
    )
    parser.add_argument(
        "--delay", type=float, default=settings.DAILY_RUN_DELAY_SECONDS,
        help="Seconds between products",
    )
    parser.add_argument("--force", action="store_true", help="Reprocess even if already in the queue")
    args = parser.parse_args()

    target_statuses = set(s.strip() for s in args.statuses.split(","))
    run_batch(args.input, args.limit, target_statuses, args.delay, args.force)
