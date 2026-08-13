"""
Deterministic (non-model) content status classification.

Used by the batch runner to decide WHICH products need drafting
without spending a model call just to make that decision -- a simple,
reliable, code-only check handles it.
"""
import re

from config import settings
from content_seo_agent.title_parser import is_regulated_product

_HTML_MARKERS_RE = re.compile(r"<p>|<ul>|<li>|<b>", re.IGNORECASE)


def _disclaimer_marker() -> str:
    """
    Derives a short, distinctive snippet from the configured disclaimer
    HTML to check for its presence, rather than requiring a second,
    separately-maintained marker string that could drift out of sync
    with config.yaml.
    """
    plain = re.sub(r"<[^>]+>", " ", settings.REGULATED_DISCLAIMER_HTML)
    words = plain.split()
    return " ".join(words[:6]).lower() if words else ""


_DISCLAIMER_MARKER = _disclaimer_marker()


def determine_content_status(title: str, description: str) -> str:
    """
    Returns one of: "missing", "thin", "plain_text_needs_formatting",
    "regulated_missing_disclaimer", "good".
    """
    description = description or ""
    desc_len = len(description)

    if desc_len == 0:
        return "missing"
    if desc_len < settings.THIN_CONTENT_CHAR_THRESHOLD:
        return "thin"
    if not _HTML_MARKERS_RE.search(description):
        return "plain_text_needs_formatting"
    if (
        is_regulated_product(title)
        and _DISCLAIMER_MARKER
        and _DISCLAIMER_MARKER not in description.lower()
    ):
        return "regulated_missing_disclaimer"
    return "good"


# Statuses where the product needs a fresh draft generated (the batch
# runner's default target).
NEEDS_DRAFTING = {"missing", "thin", "plain_text_needs_formatting"}

# Status where content is otherwise fine but just needs the disclaimer
# appended -- a code-only fix, no model/drafting call needed at all.
NEEDS_DISCLAIMER_ONLY = {"regulated_missing_disclaimer"}
