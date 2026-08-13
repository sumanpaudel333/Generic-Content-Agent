"""
Manual end-to-end test. Not a pytest suite -- run directly:

    python tests/test_pipeline_manual.py

Requires:
  - Ollama running locally with your fine-tuned model loaded (see
    config.yaml's models.small_model_name)
  - ANTHROPIC_API_KEY set in .env (needed for the escalation path)

Run from the project root so the `config` and `content_seo_agent`
packages resolve correctly.

The sample products below are generic placeholders. Swap in a few
real titles from your own catalog -- ideally including one from
your "regulated" category if you use that feature, and one
with a specific quantity in the title (e.g. "Pack of 500") -- to
exercise both the disclaimer-injection path and the quantity-mismatch
safety check with real data.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from content_seo_agent.pipeline import process_product

SAMPLE_PRODUCTS = [
    {
        "product_id": "TEST-001",
        "title": "Example Regulated Product 8% 12:1 (includes 4x20kg component)",
        "description": "",
    },
    {
        "product_id": "TEST-002",
        "title": "Example Standard Product Pack of 2000",
        "description": "",
    },
    {
        "product_id": "TEST-003",
        "title": "Another Example Regulated Item 12% 8:1 (includes 6x20kg component)",
        "description": "",
    },
]

if __name__ == "__main__":
    for product in SAMPLE_PRODUCTS:
        print("=" * 80)
        print(f"Product: {product['title']}")
        print("=" * 80)
        result = process_product(product["product_id"], product["title"], product["description"])
        print("\n--- Classification ---")
        print(json.dumps(result["classify"], indent=2, default=str))
        print("\n--- Draft ---")
        print(json.dumps(result["draft"], indent=2, default=str))
        print()

    print("Done. Check logs/review_queue.db (via the dashboard) for all queued rows,")
    print("and logs/agent_runs.log for the run history (success/escalation/failure).")
