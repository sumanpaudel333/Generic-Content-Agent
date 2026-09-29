"""
Asking the local model a question, with the notes it must answer from.

Talks to Ollama's chat endpoint directly rather than through
chat_insights/llm.py: that one forces JSON output for extraction work, and
asked "do you deliver to Cronulla?" it answered {"delivery_areas": ["Cronulla"]}.
Here we want prose.

The prompt does two jobs. It hands over the product notes for this question,
and it draws the line around what may be said. The four things the model
cannot know -- prices, stock, delivery costs and compliance claims -- are
named in the prompt AND checked in the answer afterwards, because a prompt is
an instruction, not a guarantee.
"""
import json
import logging
import re
import urllib.error
import urllib.request

from assistant import knowledge
from config import settings

logger = logging.getLogger("assistant.chat")

# The fine-tuned BC Sands model first: it writes in the house voice. The
# general model is there to compare against on the same question.
MODELS = {
    "bcsands-content-agent:latest": "BC Sands model (fine-tuned on our descriptions)",
    "content-agent:latest": "BC Sands model (earlier build)",
    "llama3.2:3b": "General model (slower, reasons better)",
}
DEFAULT_MODEL = settings.ASSISTANT_MODEL

SYSTEM_PROMPT = (
    "You are the staff assistant at {business}. You help staff answer customer questions "
    "about the products the company sells.\n\n"
    "Answer ONLY from the notes provided with each question. The notes are the approved "
    "product descriptions and the product list. If the notes do not cover it, say plainly "
    "that you do not have that information and suggest checking with the yard or the sales "
    "team. Never fill a gap with something that sounds likely.\n\n"
    "You must never state:\n"
    "- a price, a delivery fee or any cost\n"
    "- whether something is in stock, or how much is available\n"
    "- delivery times, truck access or minimum loads, unless the notes say so\n"
    "- that a product meets a standard, specification or certification\n"
    "For any of those, say it has to be confirmed with the team.\n\n"
    "Name products exactly as the notes name them, with the SKU in brackets. Keep answers "
    "short -- a few sentences, or a short list. You are talking to a colleague, not writing "
    "a brochure."
)

# What an answer must not contain, whatever the prompt said. Each is something
# the assistant cannot possibly know from the notes it was given.
_CLAIM_PATTERNS = [
    (re.compile(r"(?<![\w])\$\s?\d", re.I), "a price"),
    (re.compile(r"\b\d+\s*(?:dollars|aud)\b", re.I), "a price"),
    (re.compile(r"\bper\s+(?:tonne|ton|cubic\s+met|m3|m³|bag|load)\w*\s+(?:is|costs?|:)", re.I),
     "a price"),
    (re.compile(r"\b(?:in stock|out of stock|we have\s+\d+|stock level)\b", re.I), "stock"),
    (re.compile(r"\b(?:free delivery|delivery (?:is|costs?|fee))\b", re.I), "delivery cost"),
    # "AS4419" with no space is how the model actually wrote it when it invented
    # a standard, so the space is optional and the bare word counts too.
    (re.compile(r"\b(?:complies with|compliant with|certified to|meets? (?:australian )?standard"
                r"|australian standard|AS\s?/?\s?NZS\s?\d*|AS\s?\d{3,})\b", re.I),
     "a standards claim"),
]

TIMEOUT_SECONDS = settings.ASSISTANT_TIMEOUT_SECONDS
MAX_HISTORY_TURNS = 6


def available_models() -> dict[str, str]:
    """The models this server actually has, in the order they are preferred."""
    try:
        url = settings.OLLAMA_URL.replace("/api/chat", "/api/tags")
        with urllib.request.urlopen(url, timeout=8) as response:
            installed = {m.get("name") for m in json.loads(response.read()).get("models", [])}
    except Exception:
        logger.info("Could not list the local models; offering the configured ones")
        installed = set(MODELS)
    return {name: label for name, label in MODELS.items() if name in installed} or dict(MODELS)


def check_answer(text: str) -> str:
    """"" if the answer stayed inside what it can know, else what it claimed."""
    found = []
    for pattern, what in _CLAIM_PATTERNS:
        if pattern.search(text or "") and what not in found:
            found.append(what)
    if not found:
        return ""
    return ("This answer mentions " + ", ".join(found) +
            ", which the assistant cannot know from the product notes. Check it before "
            "repeating it to a customer.")


def _call_model(model: str, messages: list[dict], timeout: int = TIMEOUT_SECONDS) -> str:
    body = json.dumps({"model": model, "stream": False, "messages": messages,
                       "options": {"temperature": 0.2}}).encode("utf-8")
    request = urllib.request.Request(settings.OLLAMA_URL, data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())
    return ((data.get("message") or {}).get("content") or "").strip()


def build_messages(question: str, found: dict, history: list[dict] | None = None) -> list[dict]:
    system = SYSTEM_PROMPT.format(business=settings.BUSINESS_NAME)
    messages = [{"role": "system", "content": system}]
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user" if turn["role"] == "person" else "assistant",
                         "content": turn["text"]})
    messages.append({"role": "user",
                     "content": f"NOTES FOR THIS QUESTION\n{knowledge.context_text(found)}\n\n"
                                f"QUESTION\n{question}"})
    return messages


def ask(question: str, *, model: str = DEFAULT_MODEL, history: list[dict] | None = None,
        caller=None, timeout: int = TIMEOUT_SECONDS) -> dict:
    """One answer. Never raises: a model that is down or slow is reported as
    an answer the staff member can read, not a stack trace."""
    import time

    found = knowledge.find(question, described_limit=settings.ASSISTANT_DESCRIBED_PRODUCTS,
                           named_limit=settings.ASSISTANT_NAMED_PRODUCTS)
    messages = build_messages(question, found, history)
    started = time.perf_counter()
    error = ""
    try:
        answer = (caller or _call_model)(model, messages, timeout)
    except urllib.error.URLError as e:
        answer, error = "", f"The local model did not answer: {e.reason}"
    except TimeoutError:
        answer, error = "", f"The local model took longer than {timeout} seconds."
    except Exception as e:
        logger.exception("Assistant call failed")
        answer, error = "", f"The local model could not be reached: {e}"
    ms = int((time.perf_counter() - started) * 1000)

    if not answer and not error:
        error = "The model returned nothing."
    if error:
        return {"answer": "", "error": error, "ms": ms, "model": model,
                "sources": knowledge.sources(found), "flagged": "", "found": found}

    flagged = check_answer(answer)
    if not found["described"] and not found["named"]:
        flagged = flagged or ("No product matched this question, so this answer is not based on "
                              "anything in the catalogue. Treat it with suspicion.")
    return {"answer": answer, "error": "", "ms": ms, "model": model,
            "sources": knowledge.sources(found), "flagged": flagged, "found": found}
