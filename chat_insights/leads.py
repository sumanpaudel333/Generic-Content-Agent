"""
Turns analysed conversations into a lead list that does not depend on Chatbase's
outbound action firing.

Why this exists: a lead reaches the CRM through a Chatbase action that calls a
Zapier webhook. When that action does not fire, Zapier never runs, so there is
nothing on the Zapier side to reconcile against -- the lead simply vanishes.
Checking Zapier's history would only ever find Zaps that started.

So this is a second, independent path. Every lead is derived from the
conversation data already stored locally, recorded here, and alerted on from
here. A dead action then costs a duplicate entry, not a customer.

A lead needs a way to reach the customer. Buying intent with no phone number
and no email is not a lead anyone can act on -- there is nothing to follow up,
so it is left out of this list entirely and stays where it already was: counted
in the weekly report's stats, and readable in the transcripts.

Two kinds remain, which matter because they fail differently:

  form_submission  The customer filled the contact form. Chatbase HAD contact
                   details and the action was expected to fire, so a lead of
                   this type with no CRM record is the case worth chasing.
  contact_shared   They typed an email or phone into the chat instead of using
                   the form. No action would ever fire for these -- Chatbase
                   has nothing to send. Previously invisible.
"""
import logging
import re
import time

from chat_insights import db, handoff, lead_extract, redact
from config import settings

logger = logging.getLogger("chat_insights.leads")

# A phone number needs enough digits to be a phone number. The redact pattern is
# deliberately greedy (it would rather mask a quantity than leak a number); here
# a false positive becomes a bad contact detail on a lead, so it is tightened.
MIN_PHONE_DIGITS = 8
# An Australian mobile is 10 digits, a landline with country code 11-12. Longer
# than that and the pattern has run into the next thing the customer typed.
MAX_PHONE_DIGITS = 12


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _phone_key(value: str) -> str:
    """Comparable form of a phone number.

    Australian numbers arrive as 0285433400, +61285433400 and (02) 8543 3400 --
    the same number three ways. Comparing the last 9 digits makes those equal
    without needing to parse country codes.
    """
    digits = _digits(value)
    return digits[-9:] if len(digits) >= 9 else digits


def own_contacts() -> tuple[set[str], set[str]]:
    """Our own phone numbers and email addresses -- never a customer's.

    The bot quotes the branch numbers constantly: across the stored corpus one
    of them appears 60 times and another 50, while a real customer's number
    appears once or twice. Harvesting contact details without excluding these
    would tag almost every conversation as a lead offering BC Sands its own
    phone number back.

    Derived from config where possible, but branch numbers the bot happens to
    quote are not derivable from anything -- list those under
    chat_insights.own_contacts.
    """
    phones = {_phone_key(p) for p in (settings.PHONE_TEL, settings.PHONE_DISPLAY) if p}
    emails = set()
    for entry in settings.CHAT_OWN_CONTACTS:
        entry = str(entry).strip()
        if "@" in entry:
            emails.add(entry.lower())
        elif entry:
            phones.add(_phone_key(entry))
    # The address the reports are sent from is ours by definition, as is
    # anything else on its domain.
    sender = (settings.CHAT_SMTP_FROM or "").strip().lower()
    if "@" in sender:
        emails.add(sender)
    phones.discard("")
    return phones, emails


def _is_ours(*, phone: str = "", email: str = "") -> bool:
    own_phones, own_emails = own_contacts()
    if phone and _phone_key(phone) in own_phones:
        return True
    if email:
        email = email.lower()
        if email in own_emails:
            return True
        domain = email.rpartition("@")[2]
        if any(domain and e.endswith("@" + domain) for e in own_emails):
            return True
    return False


def _clean_phone(candidate: str) -> str | None:
    """The phone number inside a loose regex match, or None.

    PHONE_RE is deliberately greedy -- it would rather mask a quantity than leak
    a number -- so a match can run across a line break and pick up whatever sits
    on either side. This used to take the first line and stop, which is wrong in
    both directions: "0415368474\\n2566" puts the number first, but "postcode
    2560\\n0452127283" puts it second, and taking line one there threw away a
    real customer's phone number and lost the lead.

    So every line is considered and the first one that is actually phone-shaped
    wins.
    """
    for line in (candidate or "").splitlines():
        line = line.strip()
        if not line:
            continue
        digits = _digits(line)
        if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            return line
    return None


def _scan_text(text: str, out: dict) -> None:
    """Adds any contact details found in one piece of text. First win holds."""
    if "contact_email" not in out:
        for candidate in redact.EMAIL_RE.findall(text):
            if not _is_ours(email=candidate):
                out["contact_email"] = candidate[:200]
                break
    if "contact_phone" not in out:
        for candidate in redact.PHONE_RE.findall(text):
            cleaned = _clean_phone(candidate)
            if cleaned and not _is_ours(phone=cleaned):
                out["contact_phone"] = cleaned[:60]
                break


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
# Customers hand over their name the same way they hand over everything else --
# in the middle of a sentence, next to the phone number:
#
#     "Thanks,  Luke Clark, 0423100442, Fine Cypress mulch"
#     "Belinda mob:- 0430619277"
#     "Alice. 0487173923. From Advanz Group Pty Ltd"
#     "Sahar Zada"  /  "0432 056 330"        <- two separate messages
#
# Reading the name only out of the bot's structured read-back missed every one
# of those, and a call list that says "0423100442" instead of "Luke Clark" is
# noticeably worse to work from.
#
# The extraction is anchored rather than free: it only looks immediately around
# a phone number or email address that has already been found. A name is not
# something that can be recognised on its own -- "Marrickville", "Personal" and
# "Riviera Projects" all look exactly like one -- but the words sitting next to
# someone's phone number, in the message where they gave it, usually are.

# Labels people put in front of their own number. Stripped before the rest of
# the fragment is considered.
_CONTACT_LABEL_RE = re.compile(
    r"\b(?:mob(?:ile)?|ph(?:one)?|tel|contact|number|no|name|email|e-mail|is|its|it's)\b"
    r"\s*[:.\-]*\s*", re.I)

# Fragment separators: commas, newlines, semicolons, and full stops that are not
# part of an initial ("Sakuna s. 0424864842" splits, "M. Sharma" does not).
_FRAGMENT_RE = re.compile(r"[,\n;]+|(?<=[a-z]{2})\.\s")

# Words that are never a person's name here, whatever they look like. Mostly
# suburbs are handled by position rather than a list -- these are the ones that
# turn up adjacent to a contact detail and would otherwise pass.
_NOT_A_NAME = frozenset("""
personal business company home garden residential commercial account
yes no yep yeah nope correct right ok okay sure thanks thank cheers please
delivery deliver delivered pickup pick collect asap tomorrow today morning
afternoon evening tonight urgent
soil mulch sand turf gravel aggregate cement concrete asphalt hotmix roadbase
bag bags bulk tonne tonnes cubic meter metre litre litres pallet load truck
quote quoted quotation price pricing cost invoice order
postcode suburb address street road drive avenue site
none na nil unknown same above below
st rd ave dr cres crescent pl pde parade hwy highway ct cl tce terrace esp esplanade
blvd boulevard cct circuit
""".split())
# Street types above: a customer's address sits right next to their contact
# details, so "8 seaforth cres seaforth 2092" leaves "Seaforth Cres" looking
# exactly like a first and last name. Common surnames that double as street
# types (Lane, Way, Grove, Court, Close) are deliberately not listed.

# A company is not a person. Captured separately would be useful, but a name
# field that says "Riviera Projects" reads as a person and is not one.
_COMPANY_RE = re.compile(r"\b(?:pty|ltd|limited|inc|group|projects|services|"
                          r"landscap\w*|constructions?|builders?|contracting)\b", re.I)

_NAME_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z'\-.]*$")

# Grammar, not vocabulary. A name is a noun phrase; these are the words that
# survive when a phone number is stripped out of a sentence and the remains
# happen to look name-shaped.
_FUNCTION_WORDS = frozenset("""
a an the my your our his her their its this that these those
and or but nor so yet if then than as at by for from in into of off on onto
to with without via per about after before during over under
is are was were be been being am do does did doing have has had having
can could shall should will would may might must
prefer prefers preferred rather instead also too very just only still
please thanks thank cheers regards hi hello hey
me you he she it we they him them us
what when where which who whom why how
""".split())

# "my name is Priya Nair", "Name: Luke Clark". Unambiguous when present, and it
# survives phrasing the fragment splitter cannot ("...and my number is 0455...").
_NAME_IS_RE = re.compile(
    r"\bname\s*(?:is|=|:)\s*([A-Za-z][A-Za-z'\-.]*(?:\s+[A-Za-z][A-Za-z'\-.]*){0,2})", re.I)


def looks_like_name(candidate: str) -> bool:
    """Whether a fragment reads as a person's name.

    Strict on purpose. A wrong name is worse than none: the salesperson opens
    with it, and "Hello Personal" is a bad first impression.
    """
    candidate = (candidate or "").strip(" .,-:;\t")
    if not (2 <= len(candidate) <= 40):
        return False
    if _COMPANY_RE.search(candidate):
        return False
    tokens = candidate.split()
    if not (1 <= len(tokens) <= 3):
        return False
    for token in tokens:
        if not _NAME_TOKEN_RE.match(token):
            return False
        if token.lower().strip(".'-") in _NOT_A_NAME:
            return False
    # Capitalisation is NOT the test. Plenty of people type their name in lower
    # case -- "paul strachan, 0484536154" is a real lead from this corpus, and
    # requiring a capital dropped it. What actually separates a name from the
    # "but prefer" left over after stripping a number out of "0406858375 but
    # prefer email" is that the leftovers are ordinary English function words.
    if any(token.lower().strip(".'-") in _FUNCTION_WORDS for token in tokens):
        return False
    return True


def _stated_name(text: str) -> str:
    """A name the customer spelled out: "my name is X", "Name: X"."""
    match = _NAME_IS_RE.search(text or "")
    if not match:
        return ""
    # "my name is Priya Nair and my number is ..." captures the "and" too. A
    # name's own words are capitalised, so trailing lower-case tokens are the
    # sentence carrying on, not part of it.
    words = match.group(1).split()
    while len(words) > 1 and not words[-1][:1].isupper():
        words.pop()
    candidate = " ".join(words)
    return candidate if looks_like_name(candidate) else ""


def _name_from_text(text: str, contact_value: str) -> str:
    """The name sitting beside `contact_value` in this message, or ""."""
    if not text:
        return ""
    stated = _stated_name(text)
    if stated:
        return stated
    fragments = [f.strip() for f in _FRAGMENT_RE.split(text) if f and f.strip()]
    # The fragment holding the contact detail first, then the one before it --
    # "Thanks, Luke Clark, 0423100442" puts the name in the previous fragment,
    # "Belinda mob:- 0430619277" puts it in the same one.
    holder = next((i for i, f in enumerate(fragments) if contact_value and contact_value in f), None)
    if holder is None:
        return ""
    order = [fragments[holder]]
    if holder > 0:
        order.append(fragments[holder - 1])
    for fragment in order:
        stripped = fragment.replace(contact_value, " ")
        stripped = _CONTACT_LABEL_RE.sub(" ", stripped)
        stripped = re.sub(r"[\d()+]+", " ", stripped)       # any digits left over
        stripped = " ".join(stripped.split())
        if looks_like_name(stripped):
            return stripped
    return ""


def name_from_transcript(conv: dict, contacts: dict) -> str:
    """A person's name from the customer's own messages.

    Anchored on a contact detail already found, looking at the message it
    appeared in and the user message immediately before -- people commonly send
    their name and their number as two consecutive messages.
    """
    user_messages = [(m.get("text") or "") for m in conv.get("messages") or []
                     if (m.get("role") or "").lower() == "user"]

    # Somebody stating their name outright is the strongest signal there is, and
    # it does not need to be near a phone number to be trustworthy -- "Name:
    # Luke Clark" is often its own message, answering the bot's question.
    for text in user_messages:
        stated = _stated_name(text)
        if stated:
            return stated[:120]

    anchors = [v for v in (contacts.get("contact_phone"), contacts.get("contact_email")) if v]
    if not anchors:
        return ""
    for anchor in anchors:
        for i, text in enumerate(user_messages):
            if anchor not in text:
                continue
            found = _name_from_text(text, anchor)
            if found:
                return found[:120]
            # Name in its own message, number in the next one. Stricter here:
            # a message containing one capitalised word is far more often a
            # suburb or a product than somebody's name, so this path only
            # accepts a full name. Single-word names still come through from
            # the same message as the number ("Belinda mob:- 0430619277"),
            # where the pairing itself is the evidence.
            if i > 0:
                previous = " ".join(user_messages[i - 1].split())
                if len(previous.split()) >= 2 and looks_like_name(previous):
                    return previous[:120]

    # A bare lower-case word is normally rejected -- too many ordinary words
    # look like one. But when it matches the local part of the email address
    # they just gave, it is not a guess: "simone" after
    # simone@floorpreparationaustralia.com is their name.
    email = (contacts.get("contact_email") or "").strip().lower()
    local_part = email.rpartition("@")[0]
    if local_part:
        for text in user_messages:
            candidate = " ".join(text.split())
            if candidate and candidate.lower() == local_part and len(candidate) <= 40:
                return candidate[:120]
    return ""


def contacts_from_form(conv: dict) -> dict:
    """Contact details Chatbase collected through its form.

    Kept, but note it has never fired here: across every conversation stored,
    form_submission is null on all of them. This deployment's customers type
    their details into the chat instead, which is why the transcript and echo
    readers below carry the weight.
    """
    form = conv.get("form_submission")
    if not isinstance(form, dict):
        return {}
    out = {}
    for key in ("name", "full_name", "fullName"):
        if form.get(key):
            out["contact_name"] = str(form[key]).strip()[:120]
            break
    for key in ("email", "email_address", "emailAddress"):
        if form.get(key):
            out["contact_email"] = str(form[key]).strip()[:200]
            break
    for key in ("phone", "phone_number", "phoneNumber", "mobile"):
        if form.get(key):
            out["contact_phone"] = str(form[key]).strip()[:60]
            break
    return out


def contacts_from_transcript(conv: dict) -> dict:
    """Contact details the customer typed into the chat.

    Scanned message by message rather than from the messages joined together.
    Joining them created matches that spanned two separate messages -- a
    postcode in one and a phone number in the next came through as a single
    unusable 14-digit run -- and customers routinely send their details across
    several messages, which is exactly when that goes wrong.

    Only the customer's own messages: the bot repeats the branch phone number
    and email in almost every conversation.
    """
    out: dict = {}
    for message in conv.get("messages") or []:
        if (message.get("role") or "").lower() != "user":
            continue
        _scan_text(message.get("text") or "", out)
        if len(out) == 2:
            break
    return out


# The bot confirms what it collected in a consistent shape:
#
#     - **Name:** M Sharma
#     - **Phone:** 0452127283
#
# Worth reading because it is the only place a customer's NAME appears in a
# usable form -- "M sharma postcode 2560" typed into the chat is not something
# to parse a name out of, but the bot has already done it. Narrow on purpose:
# only this labelled shape, never free text in a bot message, because the bot
# quotes our own contact details constantly.
_ECHO_FIELD = re.compile(
    r"\*\*\s*(name|phone|mobile|contact number|email|e-mail)\s*:?\s*\*\*\s*:?\s*(.+)",
    re.I)

_ECHO_KEYS = {
    "name": "contact_name",
    "phone": "contact_phone", "mobile": "contact_phone", "contact number": "contact_phone",
    "email": "contact_email", "e-mail": "contact_email",
}


def contacts_from_bot_echo(conv: dict) -> dict:
    """Contact details the bot read back to the customer.

    Later messages win: the bot re-states the set as it grows, so the last
    summary is the most complete one.
    """
    out: dict = {}
    for message in conv.get("messages") or []:
        if (message.get("role") or "").lower() != "assistant":
            continue
        for label, raw in _ECHO_FIELD.findall(message.get("text") or ""):
            field = _ECHO_KEYS.get(label.lower())
            if not field:
                continue
            value = raw.strip().strip("*").split("(")[0].strip()
            if not value or value.lower() in ("not provided", "n/a", "none", "-"):
                continue
            if field == "contact_phone":
                cleaned = _clean_phone(value)
                if not cleaned or _is_ours(phone=cleaned):
                    continue
                out[field] = cleaned[:60]
            elif field == "contact_email":
                if _is_ours(email=value):
                    continue
                out[field] = value[:200]
            else:
                out[field] = value[:120]
    return out


FIRST_MESSAGE_CHARS = 200


def first_customer_message(conv: dict) -> str:
    """The customer's first real message, trimmed -- the enquiry line of last
    resort. Skips greetings short enough to say nothing ("hi", "hello")."""
    for message in conv.get("messages") or []:
        if (message.get("role") or "").lower() != "user":
            continue
        text = " ".join((message.get("text") or "").split())
        if len(text) < 12 and text.lower().strip("!. ") in {"hi", "hello", "hey", "hi there",
                                                             "good morning", "g'day", "gday"}:
            continue
        if text:
            return text if len(text) <= FIRST_MESSAGE_CHARS else (
                text[:FIRST_MESSAGE_CHARS - 1].rsplit(" ", 1)[0] + "…")
    return ""


def build(conv: dict, analysis: dict | None, *, refine: bool = True) -> dict | None:
    """One lead row from a conversation, or None if it is not a lead.

    The test is simply whether the customer handed over a way to contact them.
    Somebody who types their phone number into a building supplier's chat is a
    lead by any reading, and no further judgement is needed to say so.

    It used to also require the model to have ticked is_lead (or a form
    submission). That gate had to go for two reasons. It hid a real lead in the
    stored corpus that the model had not flagged. And the daily job runs before
    any model has looked at the conversation -- analyses are produced by the
    weekly pass -- so under the old rule a customer who shared their details
    this morning would stay invisible until the weekly run caught up, which is
    the exact delay this was built to remove.

    What the model says is still used where it has something to add: category,
    topic and what the customer wants. It just no longer decides whether the
    lead exists.

    Urgency comes from the hand-off state instead -- see handoff.py.
    """
    analysis = analysis or {}
    has_form = bool(conv.get("form_submission"))
    hand = handoff.inspect(conv)
    extracted = False

    # Best available source first. The bot's read-back is preferred over raw
    # customer messages for one reason: it is the only place a usable NAME
    # appears -- "M sharma postcode 2560" is not something to parse a name from,
    # but the bot has already done that work and confirmed it to the customer.
    contacts: dict = {}
    sources: list[str] = []
    # Where the name came from decides whether the model may change it: a name
    # typed into the contact form, or read back by the bot and confirmed, is the
    # customer's own statement of who they are.
    name_from = ""
    if has_form:
        form_contacts = contacts_from_form(conv)
        if form_contacts:
            contacts.update(form_contacts)
            sources.append("form")
            if contacts.get("contact_name"):
                name_from = "form"

    echo = contacts_from_bot_echo(conv)
    for key, value in echo.items():
        if key not in contacts:
            contacts[key] = value
            if key == "contact_name":
                name_from = "bot_echo"
            if "bot_echo" not in sources:
                sources.append("bot_echo")
    # Same name from the form and the read-back: the bot capitalises it
    # properly, customers often do not ("Yuan chen").
    if (name_from == "form" and echo.get("contact_name")
            and echo["contact_name"].lower() == (contacts.get("contact_name") or "").lower()):
        contacts["contact_name"] = echo["contact_name"]

    typed = contacts_from_transcript(conv)
    for key, value in typed.items():
        if key not in contacts:
            contacts[key] = value
            if "transcript" not in sources:
                sources.append("transcript")

    # Name last, and only if nothing better was found. Customers give their name
    # inline with their number far more often than the bot reads it back, so
    # without this the call list is a column of phone numbers.
    if not contacts.get("contact_name"):
        found = name_from_transcript(conv, contacts)
        if found:
            contacts["contact_name"] = found
            if "transcript" not in sources:
                sources.append("transcript")

    # No phone, no email, nothing to act on -- not even a promise we could keep.
    # Checked BEFORE the model, and that order is the whole performance story:
    # this used to run after it, so every stored chat went to the model on every
    # run -- 168 non-leads at ~9 s each, 24 minutes a run and growing daily --
    # to decide what these two lines decide for free.
    if not any(contacts.get(k) for k in ("contact_email", "contact_phone")):
        return None

    # A model second pass, over leads only. It can add a name the rules could
    # not recognise and write a better enquiry line, but every value is checked
    # against the customer's own words first -- see lead_extract.refine.
    detail_override = ""
    if refine and lead_extract.is_enabled():
        try:
            refined = lead_extract.refine(conv, contacts)
            detail_override = refined.pop("lead_detail", "")
            # The model may add a name, or tidy one the rules only guessed at.
            # It may not replace one the customer gave in the form or confirmed
            # to the bot, and nothing that fails the name test gets in: its
            # "is it in the customer's words?" check alone let "Seaforth Cres"
            # through from a typed address, over the form's "Yuan Chen".
            proposed = refined.get("contact_name")
            if proposed and proposed != contacts.get("contact_name"):
                if name_from in ("form", "bot_echo") or not looks_like_name(proposed):
                    logger.info("Kept %r over the model's name %r",
                                contacts.get("contact_name"), proposed)
                    if contacts.get("contact_name"):
                        refined["contact_name"] = contacts["contact_name"]
                    else:
                        refined.pop("contact_name", None)
            if refined != contacts:
                sources.append("model")
            contacts = refined
            extracted = True
        except Exception:
            # Refinement is an improvement, never a dependency.
            logger.exception("Lead extraction refinement failed; keeping the rule-based result")

    lead_type = "form_submission" if has_form else "contact_shared"

    return {
        "conversation_id": conv["id"],
        "run_id": conv.get("run_id"),
        "created_at": conv.get("created_at"),
        "lead_type": lead_type,
        "category": analysis.get("category") or "",
        "topic": analysis.get("topic") or "",
        # The model's summary, else the weekly analysis's, else what the
        # customer first said -- which is usually the enquiry itself, and means
        # the Inquiry line is never blank when the model was skipped or down.
        "detail": (detail_override or analysis.get("lead_detail")
                   or first_customer_message(conv)),
        "contact_source": "+".join(sources) if sources else "none",
        "extracted": extracted,
        "extracted_message_count": conv.get("message_count") or 0,
        "handoff_state": hand["state"],
        "handoff_detail": hand["detail"],
        "claim_excerpt": hand["claim_excerpt"],
        **contacts,
    }


def _needs_model(existing: dict | None, conv: dict) -> bool:
    """Whether this lead still wants the model's pass.

    Once per lead -- plus once more if the chat has grown since, because a
    customer often leaves their number first and their name a message later,
    and a chat caught mid-conversation should not keep its half-finished read.
    Leads refined before message counts were recorded count as done.
    """
    if not (existing or {}).get("extracted_at"):
        return True
    seen = existing.get("extracted_message_count")
    return seen is not None and (conv.get("message_count") or 0) > seen


def sync(run_id: int | None = None, *, conversations: list[dict] | None = None,
         refine: bool = True, model_budget_seconds: float | None = None) -> dict:
    """Records leads for stored conversations. Returns counts.

    Scans `conversations` if given (the lead job passes the recent ones),
    otherwise a run's conversations, otherwise everything stored.

    Keyed on conversations rather than on a run: analyses are cached and reused,
    so a re-run writes no analysis rows at all for conversations it has already
    seen. Anything scoped to `analyses WHERE run_id = ?` would silently miss
    those -- and a lead tool that quietly under-reports is worse than none.

    The model only ever sees a chat the rules have already found contact details
    in (see build), and only when _needs_model says so. `model_budget_seconds`
    caps the model's time in one sync: once it is spent, remaining leads are
    stored with the rules' details and tidied by the next run, so a burst of
    leads can delay nothing that matters.
    """
    if conversations is None:
        conversations = (db.list_conversations(run_id) if run_id is not None
                         else db.list_all_conversations())
    counts = {"form_submission": 0, "contact_shared": 0, "total": 0, "refined": 0,
              "deferred": 0, "scanned": len(conversations), "model_seconds": 0.0}
    budget_left = model_budget_seconds
    for conv in conversations:
        existing = db.get_lead(conv["id"])
        wants_model = refine and _needs_model(existing, conv)
        out_of_budget = wants_model and budget_left is not None and budget_left <= 0
        use_model = wants_model and not out_of_budget and lead_extract.is_enabled()

        started = time.perf_counter()
        lead = build(conv, db.get_analysis(conv["id"]), refine=use_model)
        spent = time.perf_counter() - started
        if not lead:
            continue
        if use_model:
            counts["model_seconds"] += spent
            if budget_left is not None:
                budget_left -= spent
        if out_of_budget and lead_extract.is_enabled():
            counts["deferred"] += 1
        if existing and not lead.get("extracted"):
            # Keep what the model found last time; the rules cannot improve on
            # it and would otherwise overwrite a good name with a blank.
            for field in ("contact_name", "contact_phone", "contact_email", "detail"):
                if existing.get(field):
                    lead[field] = existing[field]
        # Read, do not pop: upsert_lead needs the flag to stamp extracted_at,
        # and popping it here meant the marker was never written -- so every
        # sync looked like the first one and paid for the model again.
        if lead.get("extracted"):
            counts["refined"] += 1
        db.upsert_lead(lead)
        counts[lead["lead_type"]] += 1
        counts["total"] += 1
    counts["model_seconds"] = round(counts["model_seconds"], 1)
    # Rows recorded before contact details became a requirement, or whose
    # details have since been removed upstream. Only ever discards untouched
    # rows -- anything a person has worked stays, whatever it looks like now.
    counts["removed"] = db.purge_contactless_leads()
    logger.info("Lead sync: %s", counts)
    return counts
