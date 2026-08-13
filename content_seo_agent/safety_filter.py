"""
Post-generation safety filter.

Small fine-tuned models are prone to two specific fabrication patterns
worth guarding against explicitly, both observed during development:

1. Pattern-matching the SHAPE of a compliance claim without any real
   fact behind it -- e.g. stating "complies with [standard]" because
   similar training examples had a compliance line in that position,
   not because it's actually true for this product. Handled by
   scan_for_unverified_claims().

2. Restating a specific number from the input (a quantity, count, or
   size) incorrectly -- e.g. a digit transposition or an added zero.
   Handled by scan_for_quantity_mismatches().

This filter never tries to judge whether a claim is semantically TRUE --
it can't. It only flags that an unverified or inconsistent claim exists,
so the orchestrator can strip it, force human review, or escalate.
"""
import re

# Patterns that indicate a specific, checkable factual claim about
# standards, certification, or regulatory compliance. Extend this list
# for compliance regimes relevant to your own catalog.
CLAIM_PATTERNS = [
    re.compile(r"\bAS\s?/?\s?NZS?\s?\d{3,5}", re.IGNORECASE),   # AS 1234, AS/NZS 1234
    re.compile(r"\bAS\s?\d{3,5}", re.IGNORECASE),                 # AS3959 (no space)
    re.compile(r"\bISO\s?\d{3,6}", re.IGNORECASE),
    re.compile(r"\bcompliant with\b", re.IGNORECASE),
    re.compile(r"\bcomplies with\b", re.IGNORECASE),
    re.compile(r"\bcertified\b", re.IGNORECASE),
    re.compile(r"\bAustralian Standards?\b", re.IGNORECASE),
    re.compile(r"\bmeets? the requirements of\b", re.IGNORECASE),
]

# Numbers of 3+ digits are treated as "countable quantity" claims worth
# verifying against the title (roll counts, pack sizes, bag counts).
# Smaller numbers (percentages, ratios like "8:1") are excluded since
# those are handled by the mix-ratio extraction elsewhere, and 1-2
# digit numbers are too noisy (dimensions, counts of features, etc.)
# to check reliably.
QUANTITY_NUMBER_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{3,})\b")


def scan_for_unverified_claims(text: str, verified_claims: list[str] | None = None) -> list[str]:
    """
    Scans `text` for compliance/standards language.

    verified_claims: optional list of standard references that ARE
    legitimately confirmed for this product (e.g. pulled from
    STANDARDS_WHITELIST in config/settings.py). Any match that isn't a
    substring of one of these is flagged as unverified.

    Returns a list of the raw matched strings that were NOT verified.
    Empty list means nothing suspicious was found.
    """
    if not text:
        return []

    verified_claims = verified_claims or []
    flagged = []

    for pattern in CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            matched_text = match.group(0)
            is_verified = any(matched_text.lower() in vc.lower() for vc in verified_claims)
            if not is_verified:
                flagged.append(matched_text)

    return flagged


def scan_for_quantity_mismatches(title: str, text: str) -> list[str]:
    """
    Flags any 3+ digit number appearing in generated text that does NOT
    appear anywhere in the source title. Catches digit-transposition /
    order-of-magnitude fabrications (e.g. title says "Roll of 2000",
    draft says "roll of 20,000").

    This is intentionally coarse -- it will also flag legitimate
    derived numbers (e.g. a correctly calculated area or weight) that
    happen not to appear in the title. That's an acceptable false
    positive rate for a safety filter: it just means those go to
    review with a flag attached, not that they're blocked.
    """
    if not text:
        return []

    title_numbers = {n.replace(",", "") for n in QUANTITY_NUMBER_RE.findall(title)}
    text_numbers = QUANTITY_NUMBER_RE.findall(text)

    flagged = []
    for raw_num in text_numbers:
        normalized = raw_num.replace(",", "")
        if normalized not in title_numbers:
            flagged.append(raw_num)

    return flagged


def scan_draft_json(parsed_json: dict, title: str = "", verified_claims: list[str] | None = None) -> list[str]:
    """
    Convenience wrapper: scans every string field/list item in a
    drafting-task JSON output (overview, features, applications) for
    both unverified compliance claims and quantity mismatches against
    the source title.
    """
    if not parsed_json:
        return []

    all_flags = []
    overview = parsed_json.get("overview", "")
    all_flags.extend(scan_for_unverified_claims(overview, verified_claims))
    if title:
        all_flags.extend(scan_for_quantity_mismatches(title, overview))

    for field in ("features", "applications"):
        for item in parsed_json.get(field, []) or []:
            item_str = str(item)
            all_flags.extend(scan_for_unverified_claims(item_str, verified_claims))
            if title:
                all_flags.extend(scan_for_quantity_mismatches(title, item_str))

    return all_flags
