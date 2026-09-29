"""
Keeping the product snapshot up to date.

Reading all 4,800 active products out of Odoo takes about 8 seconds, so it is
not something to do while somebody waits for an answer. It is done on demand
from the Assistant page, and the page says how old the snapshot is -- a stale
catalogue is only a problem if nobody can see that it is stale.
"""
import logging
from datetime import datetime, timedelta, timezone

from assistant import store
from config.localtime import to_au

logger = logging.getLogger("assistant.catalogue")

STALE_AFTER = timedelta(days=7)


def refresh(connector=None) -> dict:
    """Replaces the snapshot from Odoo. Never raises: a failed refresh leaves
    the previous snapshot in place, which is better than no catalogue at all."""
    if connector is None:
        from connectors.odoo_connector import odoo_connector as connector
    if not connector.is_configured():
        return {"ok": False, "count": store.catalogue_count(),
                "detail": "Odoo is not set up on this server, so the product list cannot be read."}
    try:
        products = connector.list_products()
    except Exception as e:
        logger.exception("Could not read the product list from Odoo")
        return {"ok": False, "count": store.catalogue_count(),
                "detail": f"Could not read the product list from Odoo: {e}"}
    count = store.replace_catalogue(products)
    return {"ok": True, "count": count,
            "detail": f"{count} product(s) read from Odoo ({connector.env_name})."}


def status() -> dict:
    """What the page shows: how many products, how old, and whether that matters."""
    refreshed = store.catalogue_refreshed_at()
    moment = to_au(refreshed)
    stale = True
    if moment:
        stale = datetime.now(timezone.utc) - moment.astimezone(timezone.utc) > STALE_AFTER
    return {"count": store.catalogue_count(), "refreshed_at": refreshed,
            "stale": stale, "never": not refreshed}
