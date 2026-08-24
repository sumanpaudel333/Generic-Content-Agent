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

from chat_insights import llm
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
