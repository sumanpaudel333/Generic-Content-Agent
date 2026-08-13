"""
Deterministic, code-only extraction of facts from the product title.

This exists because these are exactly the kind of specific, checkable
facts a model should never be trusted to generate freely (see
safety_filter.py for why). Whether a product falls into the
"regulated" category, and any ratio/quantity detail worth preserving
verbatim, are extracted here with regex -- driven by config.yaml, not
hardcoded -- and injected at assembly time, never left to the model
to restate.

If your catalog has no such category, set regulated_product.enabled:
false in config.yaml and these functions become no-ops.
"""
from config import settings


def is_regulated_product(title: str) -> bool:
    if not settings.REGULATED_KEYWORD_RE:
        return False
    return bool(settings.REGULATED_KEYWORD_RE.search(title or ""))


def extract_ratio(title: str) -> str:
    if not settings.REGULATED_RATIO_RE:
        return ""
    match = settings.REGULATED_RATIO_RE.search(title or "")
    if not match:
        return ""
    groups = match.groups()
    if len(groups) == 3:
        return f"{groups[0]}% {groups[1]}:{groups[2]}"
    return match.group(0)


def extract_quantity_detail(title: str) -> str:
    if not settings.REGULATED_QUANTITY_DETAIL_RE:
        return ""
    match = settings.REGULATED_QUANTITY_DETAIL_RE.search(title or "")
    return match.group(0) if match else ""


def parse_title(title: str) -> dict:
    """Convenience: run all extractions at once."""
    return {
        "is_regulated": is_regulated_product(title),
        "ratio": extract_ratio(title),
        "quantity_detail": extract_quantity_detail(title),
    }
