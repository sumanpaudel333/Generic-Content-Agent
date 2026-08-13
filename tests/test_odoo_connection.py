"""
Run this FIRST, before setting up the daily job, to confirm the Odoo
API connection actually works. Requires ODOO_URL, ODOO_DB,
ODOO_USERNAME, ODOO_API_KEY set in .env.

Usage (from project root):
    python tests\\test_odoo_connection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from connectors.odoo_connector import odoo_connector

if __name__ == "__main__":
    print("Checking .env configuration...")
    if not odoo_connector.is_configured():
        print("FAILED: one or more of ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY "
              "is missing from .env. Fill these in and try again.")
        sys.exit(1)
    print(f"  ODOO_URL:      {odoo_connector.url}")
    print(f"  ODOO_DB:       {odoo_connector.db}")
    print(f"  ODOO_USERNAME: {odoo_connector.username}")
    print(f"  ODOO_API_KEY:  {'*' * len(odoo_connector.api_key)} (hidden)")
    print()

    print("Attempting authentication...")
    try:
        uid = odoo_connector._authenticate()
        print(f"SUCCESS: authenticated, uid={uid}")
    except Exception as e:
        print(f"FAILED to authenticate: {e}")
        print()
        print("If the error mentions 'Custom pricing plan' or a similar access "
              "restriction, this confirms the Odoo Online plan doesn't include "
              "external API access -- a fallback (manual periodic export) will "
              "be needed instead of live product pulls.")
        sys.exit(1)

    print()
    print("Attempting to list 5 products...")
    try:
        products = odoo_connector.list_products(limit=5)
        print(f"SUCCESS: retrieved {len(products)} products")
        for p in products:
            title_preview = (p["product_title"] or "")[:60]
            desc_len = len(p["product_description"] or "")
            print(f"  id={p['product_id']:<8} desc_len={desc_len:<6} {title_preview}")
    except Exception as e:
        print(f"FAILED to list products: {e}")
        sys.exit(1)

    print()
    print("All checks passed. Live Odoo product pull is working -- the daily "
          "job can source directly from Odoo instead of a static export file.")
