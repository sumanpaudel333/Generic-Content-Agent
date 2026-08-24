"""
Chatbase API client.

Uses the **v1** `get-conversations` endpoint, not v2. This is deliberate and was
established against the live account:

  * v2 (`/api/v2/agents/{id}/conversations` and `.../export`) returns 200 with
    an empty `data` array for these agents -- no conversations at all. It also
    silently returns empty for a nonexistent agent id rather than 404ing, which
    makes a misconfiguration look like a quiet "no chats this week".
  * v1 (`/api/v1/get-conversations?chatbotId=...`) returns the real
    conversations, AND supports server-side `startDate`/`endDate`, so the week
    window is filtered by the API instead of by paging through everything.

v1 paginates with `page` (1-indexed) and `size`; it returns no total and no
cursor, so paging stops on the first short page.

The response shape differs from v2 in ways that matter downstream:
  * `created_at` is an ISO-8601 string, not an epoch number
  * messages carry `content` (a plain string), not a `parts` array
  * there is **no per-message `feedback` field**, so thumbs-down cannot be read
    from the API -- unhappy customers are inferred from sentiment and the
    failure heuristics instead
  * extras worth using: `min_score` (retrieval confidence -- low means the bot
    was working from weak context) and `form_submission` (a filled contact
    form, which is a strong lead signal)
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from config import settings

logger = logging.getLogger("chat_insights.chatbase")

REQUEST_TIMEOUT_SECONDS = 45
RATE_LIMIT_BACKOFF_SECONDS = 10
MAX_PAGE_SIZE = 100

# Retrieval confidence below this suggests the bot answered from weak context.
# Used as a supporting signal only, never on its own.
LOW_SCORE_THRESHOLD = 0.5


def _describe_http_error(resp) -> str:
    """Chatbase puts the actual reason in the response body; requests'
    raise_for_status() discards it and leaves a bare
    '400 Client Error: Bad Request for url: ...' that says nothing useful.
    Always surface the body."""
    detail = ""
    try:
        payload = resp.json()
        err = payload.get("error", payload)
        if isinstance(err, dict):
            bits = [str(err.get("message", "")).strip()]
            details = err.get("details")
            if isinstance(details, dict):
                bits += [f"{k}: {v}" for k, v in details.items()]
            detail = " -- ".join(b for b in bits if b)
        else:
            detail = str(err)
    except ValueError:
        detail = (resp.text or "").strip()[:300]
    return f"HTTP {resp.status_code}" + (f" -- {detail}" if detail else "")


def api_key() -> str:
    return os.environ.get("CHATBASE_API_KEY", "")


def is_configured() -> bool:
    return bool(api_key() and settings.CHAT_AGENT_ID)


def config_status() -> str:
    if is_configured():
        return f"Chatbase: configured (agent {settings.CHAT_AGENT_ID})"
    missing = []
    if not api_key():
        missing.append("CHATBASE_API_KEY (.env)")
    if not settings.CHAT_AGENT_ID:
        missing.append("chat_insights.chatbase_agent_id (config.yaml)")
    return "Chatbase: not configured -- missing " + ", ".join(missing)


def _headers() -> dict:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _conversations_url() -> str:
    return f"{settings.CHAT_API_BASE}/get-conversations"


def list_agents() -> list[dict]:
    """Every agent this API key can see, as [{"id", "name"}].

    Worth having because the ids are opaque and easy to confuse with the API
    key -- and because v1 answers an unknown chatbotId with an empty list
    rather than an error, so a wrong id otherwise looks like "no chats".
    """
    if not api_key():
        return []
    url = settings.CHAT_API_BASE.rsplit("/api/", 1)[0] + "/api/v2/agents"
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            logger.warning("Could not list agents: %s", _describe_http_error(resp))
            return []
        return [{"id": a.get("id"), "name": a.get("name")} for a in resp.json().get("data", [])]
    except requests.RequestException as e:
        logger.warning("Could not list agents: %s", e)
        return []


def _to_epoch(value) -> int | None:
    """v1 returns ISO-8601 strings; be tolerant of epoch seconds/ms too, since
    getting this wrong would silently drop a whole week."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10_000_000_000 else v
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _message_text(message: dict) -> str:
    """v1 messages carry `content` as a plain string. The `parts` fallback keeps
    this working if an account is ever served the v2 shape."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    parts = message.get("parts") or []
    texts = [p.get("text", "") for p in parts
             if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(t for t in texts if t).strip()


def normalise_conversation(raw: dict) -> dict:
    """Flattens one API conversation into the shape the rest of this package
    uses, so nothing downstream depends on Chatbase's wire format."""
    messages = []
    negative = positive = 0
    for m in raw.get("messages") or []:
        feedback = m.get("feedback")  # absent in v1; honoured if ever present
        if feedback == "negative":
            negative += 1
        elif feedback == "positive":
            positive += 1
        messages.append({
            "id": m.get("id"),
            "role": m.get("role"),
            "text": _message_text(m),
            "created_at": _to_epoch(m.get("createdAt") or m.get("created_at")),
            "feedback": feedback,
        })

    user_messages = [m for m in messages if m["role"] == "user"]
    form = raw.get("form_submission")
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "created_at": _to_epoch(raw.get("created_at") or raw.get("createdAt")),
        "updated_at": _to_epoch(raw.get("last_message_at") or raw.get("updatedAt")),
        "user_id": raw.get("userId"),
        "source": raw.get("source"),
        "status": raw.get("status"),
        "country": raw.get("country"),
        "min_score": raw.get("min_score"),
        "low_confidence": (isinstance(raw.get("min_score"), (int, float))
                            and raw["min_score"] < LOW_SCORE_THRESHOLD),
        "form_submission": form,
        "has_form_submission": bool(form),
        "messages": messages,
        "message_count": len(messages),
        "user_message_count": len(user_messages),
        "negative_feedback": negative,
        "positive_feedback": positive,
    }


def fetch_conversations(start: datetime, end: datetime, *, max_pages: int | None = None,
                         page_size: int | None = None) -> dict:
    """Every conversation created in [start, end).

    Returns {"conversations": [...], "pages_fetched": int, "truncated": bool,
             "total_seen": int, "error": str|None}

    The API filters by date server-side, but its startDate/endDate are
    whole-day and their inclusivity is undocumented, so the exact
    [start, end) boundary is still enforced here on the timestamps. That is
    cheap -- the API has already narrowed the set -- and makes the window exact.
    """
    if not is_configured():
        return {"conversations": [], "pages_fetched": 0, "truncated": False,
                "total_seen": 0, "error": config_status()}

    max_pages = max_pages or settings.CHAT_MAX_PAGES
    page_size = min(page_size or settings.CHAT_PAGE_SIZE, MAX_PAGE_SIZE)
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())

    collected: dict[str, dict] = {}
    total_seen = 0
    pages = 0
    truncated = False
    page = 1

    while page <= max_pages:
        params = {
            "chatbotId": settings.CHAT_AGENT_ID,
            "startDate": start.strftime("%Y-%m-%d"),
            # endDate is inclusive of the day; the timestamp filter below
            # trims anything at or past the exclusive end boundary.
            "endDate": end.strftime("%Y-%m-%d"),
            "page": page,
            "size": page_size,
        }
        try:
            resp = requests.get(_conversations_url(), headers=_headers(), params=params,
                                 timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After") or RATE_LIMIT_BACKOFF_SECONDS)
                logger.warning("Chatbase rate limit hit, waiting %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                detail = _describe_http_error(resp)
                logger.error("Chatbase fetch failed on page %s: %s", page, detail)
                return {"conversations": list(collected.values()), "pages_fetched": pages,
                        "truncated": True, "total_seen": total_seen, "error": detail}
            batch = resp.json().get("data") or []
        except requests.RequestException as e:
            logger.error("Chatbase fetch failed on page %s: %s", page, e)
            return {"conversations": list(collected.values()), "pages_fetched": pages,
                    "truncated": True, "total_seen": total_seen, "error": str(e)}

        pages += 1
        total_seen += len(batch)

        for raw in batch:
            conv = normalise_conversation(raw)
            created = conv["created_at"]
            if created is None or not (start_ts <= created < end_ts):
                continue
            if conv["id"]:
                collected[conv["id"]] = conv

        # v1 gives no total and no cursor: a short page is the end of the data.
        if len(batch) < page_size:
            break
        page += 1
        if settings.CHAT_REQUEST_DELAY_SECONDS:
            time.sleep(settings.CHAT_REQUEST_DELAY_SECONDS)
    else:
        truncated = True
        logger.warning("Stopped at the %s-page cap; the window may be incomplete.", max_pages)

    conversations = sorted(collected.values(), key=lambda c: c.get("created_at") or 0)
    return {"conversations": conversations, "pages_fetched": pages, "truncated": truncated,
            "total_seen": total_seen, "error": None}


def week_window(reference: datetime | None = None, lookback_days: int | None = None
                 ) -> tuple[datetime, datetime]:
    """The window to report on: the `lookback_days` ending at midnight UTC today,
    so a run is stable regardless of what time of day it fires."""
    lookback_days = lookback_days or settings.CHAT_LOOKBACK_DAYS
    now = reference or datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=lookback_days), end
