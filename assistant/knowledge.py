"""
What the assistant is allowed to know, and how the right bits are found.

Two sources, in order of trust:

    approved descriptions   written by the Content Agent and checked by a
                            person. Full sentences, house rules already
                            applied. These are what an answer should be built
                            from wherever they exist.
    the Odoo product list   every active product's name and SKU (4,800 of
                            them), plus whatever description Odoo already
                            holds. Thin, but it is the difference between "we
                            sell that -- it is called X, SKU Y" and silence.

Matching is plain token overlap rather than embeddings. At a few thousand
products it takes milliseconds, it is testable, and when it picks the wrong
product a person can see exactly why. If the catalogue ever reaches the tens
of thousands, this is the piece to replace.
"""
import re

from assistant import store
from config import settings

# Words that match everything and therefore mean nothing here. "sand" is NOT
# in this list -- in a sand supplier's catalogue it is still a useful filter.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being am
do does did doing have has had having can could shall should will would may might must
i we you they he she it me us them my our your their
what when where which who whom why how much many any some all
for from with without into onto about after before during over under to of in on at by
need needs want wants looking look use used using suit suits suitable good best better
customer client please thanks thank hi hello hey got get
product products item items thing things one two
""".split())

TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*")
MIN_TOKEN = 3


def terms(text: str) -> list[str]:
    """The words worth matching on, singular-folded so "pavers" finds "paver"."""
    out = []
    for token in TOKEN.findall((text or "").lower()):
        if len(token) < MIN_TOKEN or token in STOPWORDS:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        out.append(token)
    return out


def _fold(text: str) -> str:
    return " ".join(terms(text))


def approved_descriptions() -> dict[str, dict]:
    """{SKU: {title, text}} for every approved description, newest per SKU."""
    from content_seo_agent import review_queue
    from content_seo_agent.constants import Status

    out: dict[str, dict] = {}
    for row in review_queue.list_rows(status=Status.APPROVED):
        sku = str(row.get("product_id") or "").strip()
        draft = row.get("parsed_output") or {}
        if not sku or not isinstance(draft, dict):
            continue
        lines = []
        if draft.get("overview"):
            lines.append(str(draft["overview"]).strip())
        for label, key in (("Features", "features"), ("Applications", "applications")):
            items = [str(i).strip() for i in (draft.get(key) or []) if str(i).strip()]
            if items:
                lines.append(f"{label}: " + "; ".join(items))
        for section in draft.get("sections") or []:
            items = [str(i).strip() for i in (section.get("items") or []) if str(i).strip()]
            if section.get("heading") and items:
                lines.append(f"{section['heading']}: " + "; ".join(items))
        if lines:
            out[sku] = {"sku": sku, "title": row.get("title") or "", "text": "\n".join(lines),
                        "row_id": row.get("id")}
    return out


def _score(question_terms: list[str], title: str, body: str) -> int:
    """Title matches count for more: a product is what its name says it is."""
    if not question_terms:
        return 0
    title_terms = set(terms(title))
    body_terms = set(terms(body))
    score = 0
    for term in set(question_terms):
        if term in title_terms:
            score += 5
        elif term in body_terms:
            score += 1
    # Every word of the question in one product's name is a near-certain match.
    if title_terms and set(question_terms) <= title_terms:
        score += 4
    return score


def find(question: str, *, described_limit: int = 4, named_limit: int = 12) -> dict:
    """The products this question is about.

    Returns {"described": [...], "named": [...]}: the ones with an approved
    description first, then other products from the catalogue that match by
    name. Both empty means the assistant should say it does not know rather
    than reach for what the model half-remembers.
    """
    question_terms = terms(question)
    described_by_sku = approved_descriptions()

    described = []
    for sku, item in described_by_sku.items():
        score = _score(question_terms, item["title"], item["text"])
        if score > 0:
            described.append({**item, "score": score})
    described.sort(key=lambda p: (-p["score"], p["title"]))
    described = described[:described_limit]
    chosen = {p["sku"] for p in described}

    named = []
    for product in store.all_products():
        if product["sku"] in chosen:
            continue
        score = _score(question_terms, product["title"], product.get("description") or "")
        if score > 0:
            named.append({"sku": product["sku"], "title": product["title"],
                          "description": (product.get("description") or "")[:400],
                          "score": score})
    named.sort(key=lambda p: (-p["score"], p["title"]))
    return {"described": described, "named": named[:named_limit]}


def business_facts() -> str:
    parts = [f"{settings.BUSINESS_NAME} is {settings.BUSINESS_DESCRIPTION}."]
    if settings.LOCATIONS:
        parts.append("Yards: " + ", ".join(settings.LOCATIONS) + ".")
    if settings.SERVICE_REGIONS:
        parts.append("Delivers around: " + ", ".join(settings.SERVICE_REGIONS) + ".")
    if settings.PHONE_DISPLAY:
        parts.append(f"Phone: {settings.PHONE_DISPLAY}.")
    return " ".join(parts)


def context_text(found: dict, *, description_chars: int = 900) -> str:
    """The notes handed to the model -- and the only thing it may answer from."""
    blocks = [f"ABOUT THE BUSINESS\n{business_facts()}"]
    if found["described"]:
        written = []
        for item in found["described"]:
            text = item["text"]
            if len(text) > description_chars:
                text = text[:description_chars].rsplit(" ", 1)[0] + "..."
            written.append(f"PRODUCT: {item['title']} (SKU {item['sku']})\n{text}")
        blocks.append("APPROVED PRODUCT DESCRIPTIONS\n" + "\n\n".join(written))
    if found["named"]:
        listed = "\n".join(f"- {p['title']} (SKU {p['sku']})" for p in found["named"])
        blocks.append("OTHER PRODUCTS WE SELL WITH MATCHING NAMES (name and code only, no "
                      "description available)\n" + listed)
    if not found["described"] and not found["named"]:
        blocks.append("NO PRODUCTS MATCHED THIS QUESTION.")
    return "\n\n".join(blocks)


def sources(found: dict) -> list[dict]:
    """What the answer was built from, for showing under it."""
    out = [{"kind": "description", "sku": p["sku"], "title": p["title"], "row_id": p.get("row_id")}
           for p in found["described"]]
    out += [{"kind": "product", "sku": p["sku"], "title": p["title"]} for p in found["named"]]
    return out
