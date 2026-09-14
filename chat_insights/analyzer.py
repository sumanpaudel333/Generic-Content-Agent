"""
Conversation analysis.

Same principle as the content pipeline: anything that can be decided in code is
decided in code, and the model is only asked for the parts that genuinely need
judgement. That matters here for a practical reason -- this host is CPU-only
with 2 cores, so every avoided generation is ~30-60 seconds saved.

Deterministic (no model):
  * volume, per-day counts, message counts, busiest day
  * negative feedback -- the API already reports it per message
  * bot-failure detection -- phrase heuristics over the transcript

Model (per conversation, tight JSON, low token cap):
  * topic / category / sentiment / lead detection / one-line summary

Themes are then grouped in Python from the model's category values rather than
asking a 3B model to summarise a hundred transcripts at once, which it would do
slowly and badly.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from chat_insights import llm, redact
from config import settings

logger = logging.getLogger("chat_insights.analyzer")

# Phrases that indicate the bot could not help. Kept explicit and readable so
# they can be tuned against real transcripts rather than hidden in a model.
FAILURE_PATTERNS = [
    re.compile(r"\bi (?:do not|don't) (?:know|have)\b", re.I),
    re.compile(r"\bi(?:'m| am) (?:not sure|unable|sorry)\b", re.I),
    re.compile(r"\bcan(?:not|'t) (?:help|assist|answer|find)\b", re.I),
    re.compile(r"\bno information\b", re.I),
    re.compile(r"\b(?:please )?contact (?:us|our team|the team|customer service)\b", re.I),
    re.compile(r"\bget in touch\b", re.I),
    re.compile(r"\bi (?:could not|couldn't) find\b", re.I),
    re.compile(r"\bbeyond (?:my|the) (?:scope|knowledge)\b", re.I),
]

VALID_SENTIMENTS = {"positive", "neutral", "negative"}

CATEGORIES = [
    "product_enquiry", "pricing", "stock_availability", "delivery",
    "order_status", "returns", "technical_advice", "account", "complaint", "other",
]

SYSTEM_PROMPT = (
    "You analyse customer support chat transcripts for a building and landscaping "
    "supplies company. Read the transcript and reply with ONLY a JSON object:\n"
    '{"topic": "<5 words or fewer naming what the customer wanted>", '
    f'"category": one of {CATEGORIES}, '
    '"resolved": true or false (did the customer get what they needed?), '
    '"sentiment": "positive" | "neutral" | "negative", '
    '"is_lead": true or false (did they show real buying intent for a specific product?), '
    '"lead_detail": "<what they want to buy, or empty string>", '
    '"summary": "<one sentence, max 25 words>"}\n'
    "Base every field only on what is in the transcript. Do not invent details."
)


def transcript_text(messages: list[dict], max_chars: int = 6000) -> str:
    """Renders messages as plain 'User:' / 'Bot:' lines for the model. Truncated
    from the START rather than the end when long -- the tail of a conversation
    (where it succeeds or fails) is more informative than the opening."""
    lines = []
    for m in messages or []:
        role = "User" if m.get("role") == "user" else "Bot"
        text = (m.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = "...[earlier turns omitted]...\n" + joined[-max_chars:]
    return joined


def detect_bot_failure(messages: list[dict], conv: dict | None = None) -> tuple[bool, str]:
    """Heuristics for 'the bot could not help'. Returns (failed, reason).

    Deliberately code, not model: these are the highest-value items in the
    report and the signal is explicit in the text, so it should not depend on a
    small model's judgement.

    Note the API gives no per-message feedback (no thumbs-up/down) on this
    account, so the phrase and behaviour heuristics carry the whole load. The
    `feedback` check below is kept for accounts where it is populated."""
    assistant_texts = [(m.get("text") or "") for m in messages if m.get("role") == "assistant"]
    user_texts = [(m.get("text") or "").strip().lower() for m in messages if m.get("role") == "user"]

    for text in assistant_texts:
        for pattern in FAILURE_PATTERNS:
            if pattern.search(text):
                return True, f"Bot replied with: “{pattern.search(text).group(0)}”"

    # A user message with no assistant reply after it -- the conversation was
    # abandoned mid-question.
    if messages and messages[-1].get("role") == "user":
        return True, "Conversation ended on an unanswered customer message"

    # The same question asked twice usually means the first answer missed.
    normalised = [re.sub(r"\W+", " ", t).strip() for t in user_texts if len(t) > 15]
    repeats = [t for t, n in Counter(normalised).items() if n > 1]
    if repeats:
        return True, "Customer repeated the same question"

    if any((m.get("feedback") == "negative") for m in messages):
        return True, "Customer left negative feedback"

    # Retrieval confidence is a weak signal on its own -- plenty of good answers
    # score low -- so it only counts when the customer also never replied, i.e.
    # a shaky answer that ended the conversation.
    if conv and conv.get("low_confidence") and len(user_texts) <= 1:
        return True, (f"Bot answered from weak context "
                       f"(relevance {conv.get('min_score'):.2f}) and the customer did not reply")

    return False, ""


def analyse_conversation(conv: dict, *, model_fn=None) -> dict:
    """Deterministic checks plus one model call. `model_fn` is injectable so
    tests can run the whole path without a model."""
    messages = conv.get("messages") or []
    bot_failed, failure_reason = detect_bot_failure(messages, conv)

    base = {
        "topic": "", "category": "other", "resolved": not bot_failed,
        "sentiment": "neutral", "is_lead": False, "lead_detail": "",
        "summary": "", "bot_failed": bot_failed, "failure_reason": failure_reason,
        "parse_ok": True,
    }

    # Nothing for a model to work with -- count it, don't spend 30s on it.
    if conv.get("user_message_count", 0) < settings.CHAT_MIN_USER_MESSAGES:
        base["summary"] = "No customer message in this conversation."
        return base

    caller = model_fn or llm.complete
    result = caller(SYSTEM_PROMPT, f"Transcript:\n{transcript_text(messages)}")

    if not result.get("parse_success") or not result.get("parsed"):
        base["parse_ok"] = False
        base["summary"] = "Automated analysis could not read this conversation."
        return base

    parsed = result["parsed"]
    category = str(parsed.get("category", "other")).strip().lower()
    sentiment = str(parsed.get("sentiment", "neutral")).strip().lower()

    base.update({
        "topic": str(parsed.get("topic", ""))[:120],
        "category": category if category in CATEGORIES else "other",
        # The heuristic overrules the model on resolution: if the bot visibly
        # said it could not help, it did not resolve, whatever the model thinks.
        "resolved": False if bot_failed else bool(parsed.get("resolved", True)),
        "sentiment": sentiment if sentiment in VALID_SENTIMENTS else "neutral",
        # A submitted contact form is hard evidence of intent, so it counts as
        # a lead regardless of what the model concluded from the text.
        "is_lead": bool(parsed.get("is_lead", False)) or conv.get("has_form_submission", False),
        "lead_detail": str(parsed.get("lead_detail", ""))[:300],
        "summary": str(parsed.get("summary", ""))[:400],
    })
    if conv.get("has_form_submission") and not base["lead_detail"]:
        base["lead_detail"] = "Submitted the contact form."
    return base


# ---------------------------------------------------------------------------
# Keywords -- what customers actually typed
# ---------------------------------------------------------------------------
# The category breakdown says a conversation was about "delivery". It cannot say
# that nine people asked about delivery to one specific suburb, which is the
# kind of thing that turns into a content change. This reads the customers' own
# words to get at that.
#
# Deterministic on purpose: no model call, so it costs nothing on a CPU-only
# host and cannot invent a theme that was not there.

# Words that carry no signal in a retail chat. The chat-specific half matters
# more than the grammatical half -- without it every week's top term is "hi".
_STOPWORDS = frozenset("""
a about after again all also am an and any are as at be because been before being
below between both but by can cant cannot come could did do does doing dont down
during each few for from further had has have having he her here hers him his how
i if in into is it its itself just like me more most much my no nor not now of off
on once only or other our out over own re s same she should so some such t than
that the their them then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your
yours
hi hey hello thanks thank please yes yep yeah ok okay okey sure cheers morning
afternoon evening good great sorry excuse pardon bye goodbye regards
want need know get got give tell ask say said looking look see want wanted need
needed able help helped question questions
im ive id ill youre thats whats theres lets dont doesnt isnt arent wasnt werent
one two three back still going make made take took put use used
per via plus etc else ever every anything something someone thing things
way ways bit lot lots any many few kind sort maybe perhaps quite really
wondering wonder interested interest possible possibly
""".split())

# Kept even though they are short or would otherwise be filtered -- these are the
# units and sizes a building-supplies customer actually types.
_KEEP_SHORT = frozenset({"m3", "m2", "kg", "mm", "cm", "gst", "ton", "bag", "bin"})

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")
# Anything a redaction pass replaced. Their placeholder words must never become
# keywords in their own right ("removed" would top every list).
_MASK_WORDS = frozenset({"email", "phone", "address", "card", "number", "removed"})


def _singular(word: str) -> str:
    """Conservative de-pluralising, so "price" and "prices" are one keyword
    rather than two half-sized ones.

    Not a stemmer -- a real one would fold "delivery" into "deliveri" and make
    the output unreadable. This only ever removes a trailing plural, and leaves
    anything it is unsure about alone: "business" and "status" end in s and are
    not plurals, so both are excluded by the ss/us/is check.
    """
    if len(word) <= 3 or not word.endswith("s"):
        return word
    if word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    return word[:-1]


# Pairs the plural rule cannot reach but that split a keyword in half every
# week. Kept deliberately short: one entry per genuinely common split, not a
# thesaurus.
_SYNONYMS = {
    "deliver": "delivery",
    "delivered": "delivery",
    "delivering": "delivery",
    "pricing": "price",
    "cost": "price",
    "costs": "price",
    "quote": "quote",
    "quoted": "quote",
    "available": "availability",
    "avail": "availability",
}


def _normalise(word: str) -> str:
    word = _singular(word)
    return _SYNONYMS.get(word, word)


def _customer_text(conv: dict) -> str:
    """Only what the customer typed. The bot's replies are this system's own
    output -- counting them would measure the script, not the demand."""
    return " ".join((m.get("text") or "") for m in (conv.get("messages") or [])
                    if (m.get("role") or "").lower() == "user")


def _terms(text: str) -> set[str]:
    """Unigrams and bigrams from one conversation, de-duplicated.

    A set, not a count: the unit of interest is "how many people asked about
    this", so one customer repeating themselves five times counts once.
    """
    # Redact first. A phone number split into tokens would otherwise surface as
    # keywords, and this text ends up in an email.
    cleaned = redact.redact(text or "").lower()
    raw = _TOKEN_RE.findall(cleaned)

    def usable(word: str) -> bool:
        return ((len(word) > 2 or word in _KEEP_SHORT) and word not in _STOPWORDS
                and not word.isdigit() and word not in _MASK_WORDS)

    terms = {_normalise(w) for w in raw if usable(w)}
    # Bigrams from ADJACENT words only, so dropping a stopword does not glue
    # together two words the customer never put side by side.
    for a, b in zip(raw, raw[1:]):
        if usable(a) and usable(b):
            terms.add(f"{_normalise(a)} {_normalise(b)}")
    return terms


def extract_keywords(conversations: list[dict], analyses: dict[str, dict], *,
                      limit: int = 12, min_conversations: int = 2) -> list[dict]:
    """Most-mentioned terms, with how many went unanswered.

    That second number is the point of the section: a term customers raise often
    AND that the bot keeps failing on is a content gap with demand attached,
    which is exactly the list worth acting on.
    """
    per_term: Counter = Counter()
    unanswered: Counter = Counter()
    seen = 0

    for conv in conversations:
        text = _customer_text(conv)
        if not text.strip():
            continue
        seen += 1
        failed = bool((analyses.get(conv["id"]) or {}).get("bot_failed"))
        for term in _terms(text):
            per_term[term] += 1
            if failed:
                unanswered[term] += 1

    if not per_term:
        return []

    # A bigram makes its parts redundant. "blue metal" appearing in nearly every
    # conversation that says "metal" means listing both is noise, so the phrase
    # wins and the loose word goes.
    bigrams = [t for t in per_term if " " in t]
    redundant: set[str] = set()
    for bigram in bigrams:
        for part in bigram.split(" "):
            if per_term[part] and per_term[bigram] >= 0.6 * per_term[part]:
                redundant.add(part)

    ranked = [
        {
            "term": term,
            "conversations": count,
            "unanswered": unanswered[term],
            "share": round(100 * count / seen) if seen else 0,
        }
        for term, count in per_term.items()
        if count >= min_conversations and term not in redundant
    ]
    # Most-asked first; ties broken by how often the bot could not help, so the
    # actionable one rises.
    ranked.sort(key=lambda k: (k["conversations"], k["unanswered"]), reverse=True)
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Why the bot could not answer
# ---------------------------------------------------------------------------
# "17 unanswered" is one number covering three different problems, each owned
# by a different person:
#
#   no_content    it said "I don't have that" -- nothing on the site covers it,
#                 so the fix is to write the page
#   weak_context  it answered from something barely relevant -- the page exists
#                 but is too thin to match, so the fix is to improve it
#   deflected     it chose not to answer and pushed the customer to phone --
#                 that is bot configuration, not content
#   other         abandoned, repeated, or explicitly disliked
#
# Derived from the stored reason string rather than recorded at detection time,
# so it works on analyses that were cached before this existed -- re-running the
# week is not required to get the breakdown.
FAILURE_KINDS = ("no_content", "weak_context", "deflected", "other")

FAILURE_KIND_LABELS = {
    "no_content": "Nothing on the site covers it",
    "weak_context": "Answered from a page too thin to match",
    "deflected": "Told the customer to phone instead",
    "other": "Abandoned, repeated, or disliked",
}

FAILURE_KIND_ACTIONS = {
    "no_content": "Write the page. These are the products to draft next.",
    "weak_context": "The page exists but does not answer the question -- add the detail.",
    "deflected": "Bot configuration, not content: it had the answer and chose not to give it.",
    "other": "Read the transcript; no single fix applies.",
}


def classify_failure(reason: str) -> str:
    """One of FAILURE_KINDS from a stored failure_reason string."""
    text = (reason or "").lower()
    if "weak context" in text:
        return "weak_context"
    if "get in touch" in text or "contact us" in text or "give us a call" in text:
        return "deflected"
    if "i don" in text or "do not have" in text or "not sure" in text or "cannot find" in text:
        return "no_content"
    return "other"


def aggregate(conversations: list[dict], analyses: dict[str, dict]) -> dict:
    """Rolls per-conversation results into the report's numbers. Pure Python --
    no model involved."""
    total = len(conversations)
    per_day = Counter()
    total_messages = 0
    negative_feedback = 0
    sentiments = Counter()
    categories = Counter()
    sources = Counter()

    failures, leads, unhappy = [], [], []

    for conv in conversations:
        created = conv.get("created_at")
        if created:
            day = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
            per_day[day] += 1
        total_messages += conv.get("message_count", 0) or 0
        negative_feedback += conv.get("negative_feedback", 0) or 0
        sources[conv.get("source") or "unknown"] += 1

        a = analyses.get(conv["id"]) or {}
        sentiments[a.get("sentiment", "neutral")] += 1
        categories[a.get("category", "other")] += 1

        entry = {
            "id": conv["id"],
            "created_at": created,
            "topic": a.get("topic") or (conv.get("title") or "(untitled)"),
            "summary": a.get("summary", ""),
            "reason": a.get("failure_reason", ""),
            "lead_detail": a.get("lead_detail", ""),
        }
        if a.get("bot_failed"):
            failures.append(entry)
        if a.get("is_lead"):
            leads.append(entry)
        if a.get("sentiment") == "negative" or (conv.get("negative_feedback") or 0) > 0:
            unhappy.append(entry)

    # Split the failures by what would actually fix them. One count of 17 sends
    # nobody anywhere; three counts route to three different jobs.
    failure_kinds = Counter(classify_failure(f.get("reason", "")) for f in failures)

    busiest_day, busiest_count = (per_day.most_common(1)[0] if per_day else ("--", 0))
    resolved_count = sum(1 for c in conversations
                          if (analyses.get(c["id"]) or {}).get("resolved"))

    return {
        "total_conversations": total,
        "total_messages": total_messages,
        "avg_messages": round(total_messages / total, 1) if total else 0,
        "per_day": dict(sorted(per_day.items())),
        "busiest_day": busiest_day,
        "busiest_day_count": busiest_count,
        "resolved_count": resolved_count,
        "unresolved_count": total - resolved_count,
        "resolution_rate": round(100 * resolved_count / total) if total else 0,
        "negative_feedback_messages": negative_feedback,
        "sentiments": dict(sentiments),
        "categories": dict(categories.most_common()),
        "sources": dict(sources.most_common()),
        "failures": sorted(failures, key=lambda e: e.get("created_at") or 0, reverse=True),
        "leads": sorted(leads, key=lambda e: e.get("created_at") or 0, reverse=True),
        "unhappy": sorted(unhappy, key=lambda e: e.get("created_at") or 0, reverse=True),
        # Computed here rather than in the reporter so it lands in stats_json --
        # a report re-sent weeks later shows the same keywords it did first time.
        "keywords": extract_keywords(conversations, analyses),
        "failure_kinds": {k: failure_kinds.get(k, 0) for k in FAILURE_KINDS
                           if failure_kinds.get(k)},
    }


CATEGORY_LABELS = {
    "product_enquiry": "Product enquiries",
    "pricing": "Pricing",
    "stock_availability": "Stock &amp; availability",
    "delivery": "Delivery",
    "order_status": "Order status",
    "returns": "Returns",
    "technical_advice": "Technical advice",
    "account": "Account",
    "complaint": "Complaints",
    "other": "Other",
}


def category_label(key: str) -> str:
    return CATEGORY_LABELS.get(key, key.replace("_", " ").title())
