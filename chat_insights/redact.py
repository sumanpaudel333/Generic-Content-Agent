"""
PII redaction for chat transcripts.

Full transcripts are kept locally so the dashboard can show a conversation in
context; what gets EMAILED is redacted, so customer contact details do not end
up sitting in an inbox (or in a forwarded copy of one).

Deliberately conservative: it over-redacts rather than under-redacts. A phone
number that survives into a manager's inbox is a real problem; a product code
that gets masked by mistake is a cosmetic one.
"""
import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Australian formats plus generic international: 0412 345 678, (02) 9158 2622,
# +61 2 9158 2622, 0291582622. Requires 8+ digits so it does not eat product
# dimensions like "900x13" or quantities like "2000".
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")

# "12 Smith Street", "3/45 George Rd", "Unit 2, 8 Pine Ave"
ADDRESS_RE = re.compile(
    r"\b\d+[A-Za-z]?(?:\s*/\s*\d+[A-Za-z]?)?\s+"
    r"[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\s+"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl|"
    r"Parade|Pde|Crescent|Cres|Highway|Hwy|Terrace|Tce|Close|Cl|Way|Boulevard|Blvd)\b\.?",
    re.IGNORECASE,
)

# 4-digit Australian postcodes only when preceded by a state, so ordinary
# 4-digit numbers ("2000 pieces") are left alone.
POSTCODE_RE = re.compile(
    r"\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\b", re.IGNORECASE)

CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

EMAIL_MASK = "[email removed]"
PHONE_MASK = "[phone removed]"
ADDRESS_MASK = "[address removed]"
CARD_MASK = "[card number removed]"


def redact(text: str) -> str:
    """Masks personal contact details in `text`.

    Order matters: cards and emails first (they contain digit/word runs the
    looser phone and address patterns would otherwise chew into)."""
    if not text:
        return text or ""

    out = CREDIT_CARD_RE.sub(CARD_MASK, text)
    out = EMAIL_RE.sub(EMAIL_MASK, out)
    out = ADDRESS_RE.sub(ADDRESS_MASK, out)
    out = POSTCODE_RE.sub(lambda m: f"{m.group(1)} [postcode removed]", out)
    out = PHONE_RE.sub(PHONE_MASK, out)
    return out


def redact_conversation(messages: list[dict]) -> list[dict]:
    """Redacts the text of every message, leaving structure/roles intact."""
    cleaned = []
    for m in messages or []:
        copy = dict(m)
        copy["text"] = redact(m.get("text", ""))
        cleaned.append(copy)
    return cleaned


def contains_pii(text: str) -> bool:
    """True if anything redact() would mask is present. Used to flag which
    conversations carry contact details -- handy for lead follow-up, since a
    lead with a phone number in it is worth more than one without."""
    if not text:
        return False
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text)
                or ADDRESS_RE.search(text) or CREDIT_CARD_RE.search(text))
