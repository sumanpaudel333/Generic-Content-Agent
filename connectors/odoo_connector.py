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
import logging
import os
import re
import socket
import ssl
import xmlrpc.client
from abc import ABC, abstractmethod
from urllib.parse import urlparse

logger = logging.getLogger("connectors.odoo")

# How long a failure message may be before it is cut. It ends up in three
# places -- the card on the review page, the activity log, and the URL of the
# redirect that reports it -- and the last of those is the hard limit: IIS in
# front of this dashboard rejects any query string over 2,048 bytes, and a
# URL-encoded traceback triples in size. A whole Odoo traceback was 3,000
# characters before encoding and took the page down with a 404.15.
MAX_ERROR_CHARS = 240


def summarise_error(err) -> str:
    """One short sentence a reviewer can act on, from an exception or from a
    failure message already stored.

    Odoo reports server-side problems as an XML-RPC Fault whose text is the
    entire Python traceback from its own server. That is useful in a log and
    useless on a page: nobody reviewing product copy can do anything with
    psycopg2's call stack. So the common cases are named, and anything else
    falls back to the last line of the traceback, which is where Python puts
    the actual error.

    Accepts a string too, so messages saved before this existed can be tidied
    for display without rewriting what was stored.
    """
    if isinstance(err, xmlrpc.client.Fault):
        text = str(err.faultString or "")
    else:
        text = str(err or "")
    lowered = text.lower()

    # Odoo's database was not there to answer. On an Odoo.sh staging build this
    # is almost always the build asleep or being recreated, and it clears up
    # on its own -- worth saying, because otherwise it reads as permanent.
    if any(marker in lowered for marker in (
            "psycopg2", "registries[db_name]", "could not connect to server",
            "database \"", "does not exist", "sql_db.py")):
        return ("Odoo's database did not respond. The staging server may have been "
                "asleep or restarting, which usually clears within a minute -- "
                "try sending it again.")
    if any(marker in lowered for marker in (
            "access denied", "accessdenied", "invalid login", "wrong login",
            "authentication failed")):
        return "Odoo refused the login. Check ODOO_USERNAME and ODOO_API_KEY in .env."
    if isinstance(err, (socket.timeout, TimeoutError)) or "timed out" in lowered:
        return "Odoo took too long to answer. Try again shortly."
    if isinstance(err, (ConnectionError, socket.gaierror, ssl.SSLError)) or any(
            marker in lowered for marker in ("connection refused", "name or service not known",
                                              "getaddrinfo failed", "ssl")):
        return "Could not reach the Odoo server at all. Check the network and ODOO_URL."

    # Otherwise the most useful single line of a traceback is its last one.
    lines = [ln.strip() for ln in text.replace("\\n", "\n").splitlines() if ln.strip()]
    tail = lines[-1] if lines else text.strip()
    # Remove only the "<Fault 1: '" ... "'>" wrapper XML-RPC puts around the
    # text. Stripping every quote character took the closing quote off
    # ordinary messages -- "KeyError: 'foo'" came back as "KeyError: 'foo".
    tail = re.sub(r"^<Fault\s+\d+:\s*'?", "", tail)
    tail = re.sub(r"'?>\s*$", "", tail).strip()
    if len(tail) > MAX_ERROR_CHARS:
        tail = tail[:MAX_ERROR_CHARS - 1].rstrip() + "\u2026"
    return tail or "Odoo returned an error with no message."


def _failure(env_name: str, what: str, err: Exception) -> str:
    """The stored and displayed message, with the full detail logged."""
    logger.error("Odoo (%s) %s failed", env_name, what, exc_info=err)
    return f"Odoo ({env_name}) {what} failed: {summarise_error(err)}"

# product.template field the assembled HTML gets written to. Deliberately
# NOT description_sale ("Sales Description") -- that's a plain `text`
# field shown on quotes/sales orders, not the website, and being plain
# text it can't render HTML at all (tags show up literally). This is
# `html`-typed and is what actually renders on the eCommerce product page.
# Confirmed via product.template.fields_get() against the real instance
# rather than assumed from general Odoo docs, since this can vary by
# version/install.
DESCRIPTION_FIELD = "description_ecommerce"

# Odoo stores images as base64 in binary fields. The main product shot lives on
# product.template.image_1920; every additional one is its own
# product.template.image record pointing back at the template. Both fields are
# named for their maximum edge in pixels -- Odoo downscales anything larger on
# write and generates its own thumbnails, so nothing here needs to resize.
MAIN_IMAGE_FIELD = "image_1920"
EXTRA_IMAGE_MODEL = "product.template.image"


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

    # Images are optional capability, not part of the required contract: a
    # connector written against another platform before this existed keeps
    # working, and reports honestly that it cannot take images rather than
    # failing at publish time.
    def supports_images(self) -> bool:
        return False

    def write_product_images(self, product_id, images: list[dict]) -> dict:
        """images: [{'ref', 'name', 'b64', 'position'}], position 0 = main shot.

        Returns {'success', 'dry_run', 'detail', 'results': [{'ref', 'success',
        'odoo_image_id', 'detail'}]} -- per image, because a partial failure has
        to be recorded per image or the retry would re-send the ones that
        already landed.
        """
        return {"success": False, "dry_run": True, "results": [],
                 "detail": "This connector does not support image upload."}

    def get_product_image_state(self, product_id) -> dict | None:
        """What images the platform actually holds for this product, read back
        after a write. Returns {'has_main': bool, 'extra_ids': set[int]} or None
        if it cannot be determined."""
        return None


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
            return {"success": False, "dry_run": False,
                     "detail": _failure(self.env_name, "write", e)}

    # ---- Images ---------------------------------------------------------
    def supports_images(self) -> bool:
        return True

    def write_product_images(self, product_id, images: list[dict]) -> dict:
        if not images:
            return {"success": True, "dry_run": False, "results": [],
                     "detail": "No images to write."}
        if not self.is_configured():
            return {
                "success": False, "dry_run": True, "results": [],
                "detail": f"Odoo ({self.env_name}) not configured -- no image write attempted.",
            }
        try:
            odoo_id = self._find_by_sku(product_id)
            if odoo_id is None:
                return {
                    "success": False, "dry_run": False, "results": [],
                    "detail": f"Odoo ({self.env_name}) image write failed: no product found "
                               f"with Internal Reference (SKU) '{product_id}'",
                }
            uid = self._authenticate()
            models = self._models()
        except Exception as e:
            return {"success": False, "dry_run": False, "results": [],
                     "detail": _failure(self.env_name, "image write", e)}

        results = []
        for img in sorted(images, key=lambda i: i.get("position", 0)):
            # Each image is written on its own so one bad file cannot cost the
            # rest of the set. What landed is recorded per image, so a retry
            # sends only what is still missing.
            try:
                if img.get("position", 0) == 0:
                    models.execute_kw(
                        self.db, uid, self.api_key,
                        "product.template", "write",
                        [[odoo_id], {MAIN_IMAGE_FIELD: img["b64"]}],
                    )
                    results.append({"ref": img.get("ref"), "success": True,
                                     "odoo_image_id": odoo_id,
                                     "detail": f"Set as main image on product id={odoo_id}"})
                else:
                    new_id = models.execute_kw(
                        self.db, uid, self.api_key,
                        EXTRA_IMAGE_MODEL, "create",
                        [{"name": img.get("name") or "image",
                           MAIN_IMAGE_FIELD: img["b64"],
                           "product_tmpl_id": odoo_id}],
                    )
                    results.append({"ref": img.get("ref"), "success": True,
                                     "odoo_image_id": int(new_id),
                                     "detail": f"Added to gallery as {EXTRA_IMAGE_MODEL} id={new_id}"})
            except Exception as e:
                logger.error("Odoo (%s) image %s failed", self.env_name,
                             img.get("name") or img.get("ref"), exc_info=e)
                results.append({"ref": img.get("ref"), "success": False,
                                 "odoo_image_id": None, "detail": summarise_error(e)})

        ok = sum(1 for r in results if r["success"])
        failed = len(results) - ok
        detail = f"{ok} image(s) written to Odoo ({self.env_name}) sku={product_id}"
        if failed:
            detail += f", {failed} failed"
        return {"success": failed == 0, "dry_run": False, "results": results, "detail": detail}

    def get_product_image_state(self, product_id) -> dict | None:
        """Reads back what Odoo holds, so "we uploaded it" can be checked
        against "it is there" before the local copy is deleted."""
        if not self.is_configured():
            return None
        try:
            odoo_id = self._find_by_sku(product_id)
            if odoo_id is None:
                return None
            uid = self._authenticate()
            models = self._models()
            # Fetching the image payload itself would pull megabytes per
            # product for a boolean answer; this asks only whether it is set.
            tmpl = models.execute_kw(
                self.db, uid, self.api_key,
                "product.template", "read",
                [[odoo_id]], {"fields": ["id"]},
            )
            has_main = bool(models.execute_kw(
                self.db, uid, self.api_key,
                "product.template", "search_count",
                [[["id", "=", odoo_id], [MAIN_IMAGE_FIELD, "!=", False]]],
            )) if tmpl else False
            extra_ids = models.execute_kw(
                self.db, uid, self.api_key,
                EXTRA_IMAGE_MODEL, "search",
                [[["product_tmpl_id", "=", odoo_id]]],
            )
            return {"has_main": has_main, "extra_ids": set(int(i) for i in extra_ids)}
        except Exception:
            # Verification is best-effort by design: a failed read-back leaves
            # images unverified, which only means their local copy is kept.
            return None


# Single shared instance for the app to import
odoo_connector = OdooConnector()
