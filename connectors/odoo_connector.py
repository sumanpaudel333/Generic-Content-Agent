"""
Odoo connector.

Abstract base + XML-RPC implementation. Falls back to DRY RUN mode
automatically if the active environment's URL/DB/USERNAME/API_KEY
aren't resolvable from .env -- this lets the publish flow be tested
end-to-end (assembly, dashboard, status tracking) before real Odoo
credentials are wired in, without any risk of a misconfigured write.

Staging vs live: set ODOO_ENV=staging|live in .env to pick which
instance to talk to, with the URL for each coming from
ODOO_URL_STAGING / ODOO_URL_LIVE. Defaults to "staging" when unset,
so a missing/blank ODOO_ENV can never accidentally point at the live
catalog. ODOO_DB is optional -- if not set explicitly, it's derived
from the active URL's subdomain (Odoo Online databases are named
after their *.odoo.com / *.dev.odoo.com subdomain), which also means
it keeps working after a custom-domain migration since the
underlying database name doesn't change, only the public URL does.
Set ODOO_DB explicitly once you know it to stop relying on that
derivation.
"""
import os
import re
import xmlrpc.client
from abc import ABC, abstractmethod
from urllib.parse import urlparse

# product.template field the assembled HTML gets written to. Deliberately
# NOT description_sale ("Sales Description") -- that's a plain `text`
# field shown on quotes/sales orders, not the website, and being plain
# text it can't render HTML at all (tags show up literally). This is
# `html`-typed and is what actually renders on the eCommerce product page.
# Confirmed via product.template.fields_get() against the real instance
# rather than assumed from general Odoo docs, since this can vary by
# version/install.
DESCRIPTION_FIELD = "description_ecommerce"


def _derive_db_name(url: str) -> str:
    """Odoo Online database names match the subdomain, e.g.
    https://bcsands.odoo.com -> "bcsands", or
    https://bcsands-wmssofttest08082026-36107756.dev.odoo.com -> the
    full "bcsands-wmssofttest08082026-36107756" staging db name.
    Returns "" if the URL doesn't look like an Odoo Online address
    (e.g. a self-hosted domain), where the db name can't be guessed."""
    host = (urlparse(url).netloc or "").split(":")[0]
    match = re.match(r"^(.+?)\.(?:dev\.odoo\.com|odoo\.com)$", host)
    return match.group(1) if match else ""


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
        self.env_name = os.environ.get("ODOO_ENV", "staging").strip().lower()
        if self.env_name == "live":
            self.url = os.environ.get("ODOO_URL_LIVE", "")
        else:
            self.url = os.environ.get("ODOO_URL_STAGING", "")
        self.db = os.environ.get("ODOO_DB", "") or _derive_db_name(self.url)
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

    def _find_by_sku(self, sku) -> int | None:
        """Every product_id flowing through this system is a SKU (Odoo
        calls this field "Internal Reference", stored as default_code) --
        never Odoo's own internal numeric id, which is a separate,
        unrelated value. This resolves SKU -> internal id right before
        any read/write, so callers never have to think about the
        distinction."""
        uid = self._authenticate()
        models = self._models()
        ids = models.execute_kw(
            self.db, uid, self.api_key,
            "product.template", "search",
            [[["default_code", "=", str(sku)]]],
            {"limit": 1},
        )
        return ids[0] if ids else None

    def get_product(self, product_id) -> dict | None:
        if not self.is_configured():
            return None
        uid = self._authenticate()
        models = self._models()
        results = models.execute_kw(
            self.db, uid, self.api_key,
            "product.template", "search_read",
            [[["default_code", "=", str(product_id)]]],
            {"fields": ["id", "name", "default_code", DESCRIPTION_FIELD], "limit": 1},
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
        which source it came from. product_id here is the SKU
        (default_code / "Internal Reference"), same identifier scheme
        the Pronto backlog export uses -- so review queue rows stay
        addressable the same way regardless of which source produced
        them, and no reprocessing is needed when the backlog source
        eventually gets fully replaced by this live pull. Products with
        no Internal Reference set are skipped -- there'd be no way to
        write back to them later under this scheme.
        """
        if not self.is_configured():
            raise RuntimeError("Odoo not configured -- cannot list_products()")

        uid = self._authenticate()
        models = self._models()
        kwargs = {"fields": ["id", "default_code", "name", DESCRIPTION_FIELD], "offset": offset}
        if limit is not None:
            kwargs["limit"] = limit

        results = models.execute_kw(
            self.db, uid, self.api_key,
            "product.template", "search_read",
            [[["active", "=", True]]],
            kwargs,
        )
        products = []
        skipped_no_sku = 0
        for r in results:
            sku = r.get("default_code")
            if not sku:
                skipped_no_sku += 1
                continue
            products.append({
                "product_id": sku,
                "product_title": r.get("name", ""),
                "product_description": r.get(DESCRIPTION_FIELD) or "",
            })
        if skipped_no_sku:
            print(f"NOTE: skipped {skipped_no_sku} Odoo product(s) with no Internal Reference (SKU) set.")
        return products

    def write_product_description(self, product_id, html_description: str) -> dict:
        if not self.is_configured():
            return {
                "success": False,
                "dry_run": True,
                "detail": f"Odoo ({self.env_name}) not configured -- .env missing one of "
                           f"ODOO_URL_{self.env_name.upper()}/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY "
                           f"(ODOO_DB can be left blank if the URL is a *.odoo.com address). "
                           f"No write attempted -- this is expected until real credentials are added.",
            }
        try:
            odoo_id = self._find_by_sku(product_id)
            if odoo_id is None:
                return {
                    "success": False, "dry_run": False,
                    "detail": f"Odoo ({self.env_name}) write failed: no product found with "
                               f"Internal Reference (SKU) '{product_id}'",
                }
            uid = self._authenticate()
            models = self._models()
            models.execute_kw(
                self.db, uid, self.api_key,
                "product.template", "write",
                [[odoo_id], {DESCRIPTION_FIELD: html_description}],
            )
            return {"success": True, "dry_run": False,
                     "detail": f"Written to Odoo ({self.env_name}) sku={product_id} (id={odoo_id})"}
        except Exception as e:
            return {"success": False, "dry_run": False, "detail": f"Odoo ({self.env_name}) write failed: {e}"}


# Single shared instance for the app to import
odoo_connector = OdooConnector()
