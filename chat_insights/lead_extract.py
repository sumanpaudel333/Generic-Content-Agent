"""
Model-assisted refinement of a lead's contact details.

The deterministic reader in leads.py finds the phone number and the email
reliably -- those have a shape. Names and the enquiry do not, and that is where
it runs out of road. These three fragments are the same shape:

    "paul strachan"     a name
    "but prefer"        the remains of a sentence with the number cut out
    "garden stakes"     a product

Telling them apart needs to know what words mean, which a rule cannot and a
model can. So a model gets a second pass -- but only over conversations already
identified as leads, which is a handful a day rather than every conversation.

Three properties make this safe to depend on:

  It cannot make things worse. The deterministic result goes in, and anything
  the model fails to improve comes back out unchanged. Model down, slow, or
  talking nonsense -- the lead is still the lead it was.

  It cannot invent contact details. A model asked for a phone number will
  produce one whether or not there was one. So every value it returns is
  checked back against the customer's own words: a phone or email must appear
  there digit for digit, and a name must appear as text. The model's job is to
  POINT AT the right span, not to author it. Anything that fails the check is
  dropped and the deterministic value stands.

  It only reads the customer. The bot's messages are excluded, exactly as in
  the deterministic reader -- it quotes the branch phone number constantly.
"""
import json
import logging
import re

from chat_insights import llm
from config import settings

logger = logging.getLogger("chat_insights.lead_extract")

SYSTEM_PROMPT = (
    "You extract contact details from a customer's messages to a building "
    "supplies company. Reply with JSON only, exactly these four keys:\n"
    '{"name": "", "phone": "", "email": "", "inquiry": ""}\n'
    "Rules:\n"
    "- Copy name, phone and email EXACTLY as they appear in the messages. "
    "Never reformat, correct or complete them.\n"
    "- If something is not stated, leave that field as an empty string. Do "
    "NOT write \"empty\", \"none\", \"N/A\" or any other stand-in word, and "
    "never guess.\n"
    "- The name is the person's name -- not a company, suburb, product or "
    "greeting.\n"
    "- The inquiry is your own short summary of what they want, and should "
    "mention any order or reference number they gave."
)

_MAX_TRANSCRIPT_CHARS = 4000

# Words a model reaches for instead of leaving a field blank -- several of them
# lifted straight out of this module's own prompt. One of these got through the
# "is it in the transcript?" check by coincidence: a customer wrote "you don't
# have a empty drum", the model answered "empty" for both name and email, and
# the substring test found it. A conversation with no contact details became a
# lead whose email was the word "empty".
_PLACEHOLDERS = frozenset("""
empty none null nil na n/a nan blank unknown unspecified unstated
notstated notgiven notprovided notavailable nothing missing
customer user name email phone number person
""".split())


def _is_placeholder(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return True
    # Digits mean real content. Stripping non-letters first turned every phone
    # number into the empty string, and so into a "placeholder" -- which threw
    # away the one field the model is most reliable about.
    if any(ch.isdigit() for ch in text):
        return False
    letters = re.sub(r"[^a-z]", "", text)
    return not letters or letters in _PLACEHOLDERS


# A contact detail has to BE one. The transcript check proves the model did not
# invent the text; these prove the text is actually an email or a phone number.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_MIN_PHONE_DIGITS = 8
_MAX_PHONE_DIGITS = 12


def is_enabled() -> bool:
    return bool(settings.CHAT_LEAD_EXTRACT_ENABLED)


def _customer_text(conv: dict) -> str:
    return "\n".join(
        (m.get("text") or "").strip()
        for m in conv.get("messages") or []
        if (m.get("role") or "").lower() == "user" and (m.get("text") or "").strip()
    )


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _call_model(transcript: str) -> dict | None:
    """Runs the configured model. Returns the parsed dict, or None."""
    prompt = f"Customer messages:\n{transcript}"
    backend = (settings.CHAT_LEAD_EXTRACT_MODEL or "").strip().lower()

    if backend == "claude":
        # Imported lazily: the local-model path must not need an API key, and
        # the content agent's Claude client pulls in its own settings.
        from content_seo_agent import claude_client
        try:
            response = claude_client._call_claude(SYSTEM_PROMPT, prompt)
            raw = "".join(block.get("text", "") for block in response.get("content", []))
            parsed, ok = claude_client._extract_json(raw)
            return parsed if ok else None
        except Exception as e:
            logger.warning("Claude lead extraction failed: %s", e)
            return None

    result = llm.complete(SYSTEM_PROMPT, prompt,
                          model=settings.CHAT_LEAD_EXTRACT_LOCAL_MODEL or None,
                          temperature=0)
    return result.get("parsed") if result.get("parse_success") else None


def refine(conv: dict, found: dict) -> dict:
    """Improves `found` using the model. Returns a new dict; never raises.

    `found` is whatever the deterministic reader produced. Every key it already
    holds is kept unless the model offers something that passes validation --
    and for phone and email it has to match what the deterministic reader found
    anyway, so in practice the model only ever adds a name and the enquiry.
    """
    out = dict(found)
    if not is_enabled():
        return out

    transcript = _customer_text(conv)
    if not transcript.strip():
        return out
    transcript = transcript[:_MAX_TRANSCRIPT_CHARS]

    parsed = _call_model(transcript)
    if not isinstance(parsed, dict):
        return out

    haystack = transcript.lower()
    haystack_digits = _digits(transcript)

    # --- name -------------------------------------------------------------
    # Accepted only if the customer actually typed it. That rules out a model
    # inferring "Paul" from an email address, or politely inventing one.
    name = str(parsed.get("name") or "").strip()[:120]
    if name and _is_placeholder(name):
        name = ""
    if name and name.lower() in haystack:
        out["contact_name"] = name
    elif name:
        logger.info("Discarded model name %r: not in the customer's messages", name)

    # --- phone and email --------------------------------------------------
    # The deterministic reader is already good at these, so the model is only
    # allowed to FILL A GAP, never to overwrite. Both are checked against the
    # transcript: a phone by its digits, so formatting differences do not
    # matter, an email verbatim.
    phone = str(parsed.get("phone") or "").strip()[:60]
    if phone and _is_placeholder(phone):
        phone = ""
    if phone and not out.get("contact_phone"):
        if (_MIN_PHONE_DIGITS <= len(_digits(phone)) <= _MAX_PHONE_DIGITS
                and _digits(phone) in haystack_digits):
            out["contact_phone"] = phone
        else:
            logger.info("Discarded model phone %r: not in the customer's messages", phone)

    email = str(parsed.get("email") or "").strip()[:200]
    if email and _is_placeholder(email):
        email = ""
    if email and not out.get("contact_email"):
        if _EMAIL_SHAPE.match(email) and email.lower() in haystack:
            out["contact_email"] = email
        else:
            logger.info("Discarded model email %r: not in the customer's messages", email)

    # --- inquiry ----------------------------------------------------------
    # A summary, so it cannot be checked against the text the way the others
    # can -- it is capped instead, and it is the one field where a slightly
    # wrong answer costs nothing: the transcript link is right beside it.
    inquiry = " ".join(str(parsed.get("inquiry") or "").split())[:300]
    if _is_placeholder(inquiry):
        inquiry = ""
    if inquiry:
        out["lead_detail"] = inquiry

    return out


def status() -> tuple[bool, str]:
    """(ok, detail) for the dashboard and --dry-run."""
    if not is_enabled():
        return True, "disabled (chat_insights.lead_extraction.enabled)"
    backend = (settings.CHAT_LEAD_EXTRACT_MODEL or "").strip().lower()
    if backend == "claude":
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "set to claude but ANTHROPIC_API_KEY is not in .env"
        return True, f"{settings.CLAUDE_MODEL} via the Anthropic API"
    model = settings.CHAT_LEAD_EXTRACT_LOCAL_MODEL or settings.CHAT_MODEL
    ok, detail = llm.is_available(model)
    return ok, (f"{model} via Ollama" if ok else detail)
