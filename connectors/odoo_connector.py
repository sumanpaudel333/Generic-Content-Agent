"""
Odoo connector.

Abstract base + XML-RPC implementation. Falls back to DRY RUN mode
automatically if ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY aren't
set in .env -- this lets the publish flow be tested end-to-end
(assembly, dashboard, status tracking) before real Odoo credentials
are wired in, without any risk of a misconfigured write.
"""
import os
import xmlrpc.client
from abc import ABC, abstractmethod


class BaseConnector(ABC):
    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def write_product_description(self, product_id, html_description: str) -> dict:
        """Returns {'success': bool, 'dry_run': bool, 'detail': str}"""
        ...

    @abstractmethod
    def get_product(self, product_id) -> dict | None:
        ...


class OdooConnector(BaseConnector):
    def __init__(self):
        self.url = os.environ.get("ODOO_URL", "")
        self.db = os.environ.get("ODOO_DB", "")
        self.username = os.environ.get("ODOO_USERNAME", "")
        self.api_key = os.environ.get("ODOO_API_KEY", "")
        self._uid = None

    def is_configured(self) -> bool:
        return bool(self.url and self.db and self.username and self.api_key)

    def _authenticate(self):
        if self._uid is not None:
            return self._uid
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._uid = common.authenticate(self.db, self.username, self.api_key, {})
        if not self._uid:
            raise RuntimeError("Odoo authentication failed -- check ODOO_USERNAME/ODOO_API_KEY")
        return self._uid

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def get_product(self, product_id) -> dict | None:
        if not self.is_configured():
            return None
        uid = self._authenticate()
        models = self._models()
        results = models.execute_kw(
            self.db, uid, self.api_key,
            "product.template", "read",
            [[int(product_id)]],
            {"fields": ["id", "name", "description_sale"]},
        )
        return results[0] if results else None

    def list_products(self, limit: int | None = None, offset: int = 0) -> list[dict]:
        """
        Live pull of products from Odoo, used by the daily job to detect
        new/changed products without needing a manual export. Pulls the
        full active product list each run rather than filtering by
        creation date -- dedup against the review queue already handles
        "is this new" more robustly than a timestamp would (self-heals
        if a day's run gets skipped, no separate bookkeeping needed).

        Returns a list of {"product_id", "product_title", "product_description"}
        dicts, matching the shape batch_runner.load_products() produces
        from a static file, so downstream code doesn't need to care
        which source it came from.
        """
        if not self.is_configured():
            raise RuntimeError("Odoo not configured -- cannot list_products()")

        uid = self._authenticate()
        models = self._models()
        kwargs = {"fields": ["id", "name", "description_sale"], "offset": offset}
        if limit is not None:
            kwargs["limit"] = limit

        results = models.execute_kw(
            self.db, uid, self.api_key,
            "product.template", "search_read",
            [[["active", "=", True]]],
            kwargs,
        )
        return [
            {
                "product_id": r["id"],
                "product_title": r.get("name", ""),
                "product_description": r.get("description_sale") or "",
            }
            for r in results
        ]

    def write_product_description(self, product_id, html_description: str) -> dict:
        if not self.is_configured():
            return {
                "success": False,
                "dry_run": True,
                "detail": "Odoo not configured (.env missing ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY). "
                           "No write attempted -- this is expected until real credentials are added.",
            }
        try:
            uid = self._authenticate()
            models = self._models()
            models.execute_kw(
                self.db, uid, self.api_key,
                "product.template", "write",
                [[int(product_id)], {"description_sale": html_description}],
            )
            return {"success": True, "dry_run": False, "detail": f"Written to Odoo product_id={product_id}"}
        except Exception as e:
            return {"success": False, "dry_run": False, "detail": f"Odoo write failed: {e}"}


# Single shared instance for the app to import
odoo_connector = OdooConnector()
