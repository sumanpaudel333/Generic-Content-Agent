"""
Did the bot actually forward the lead, or only say it did?

The problem this solves, in one conversation from the live corpus:

    [16] user       0452127283
    [17] assistant  "Perfect! I have all your details now:
                       Name: M Sharma
                       Phone: 0452127283 ...
                     Thank you. I have forwarded your details and request to
                     our sales team for a callback."
                    <-- conversation ends here

Nothing was forwarded. The customer was told it had been, gave a phone number,
and was never contacted. There is no trace of this on the Zapier side either,
because the action never started -- so reconciling against Zapier's history
finds nothing to reconcile. The only place the failure is visible is the
message stream itself.

How the stream gives it away
----------------------------
When an action really runs, Chatbase records it as three consecutive messages:

    assistant   ""                    <- empty, carries the tool call
    tool        ""                    <- the action result
    assistant   "Great, I've ..."     <- what the customer actually sees

Only that third message is visible in the chat. The first two are the machinery.
So a hand-off claim with those two messages in front of it is real, and a claim
without them is the bot narrating an action that never ran.

Checked against every conversation held locally: 16 of 16 tool messages are
preceded by an empty assistant message, and there are no empty assistant
messages that are not followed by a tool message. The pair is reliable enough to
detect on, and detecting on either half gives the same answer.

Adjacency is the whole test, and it has to be. A conversation can contain an
earlier action for something else entirely -- the delivery calculator uses the
same mechanism -- so "this conversation contains a tool call somewhere" says
nothing about whether the lead was forwarded. What matters is whether one sits
immediately before the sentence claiming it did.

Deliberately no model involved. This is a structural property of the message
list, a local model would be slower and less certain about it, and the daily
lead job can then run without Ollama being up.
"""
import logging
import re

logger = logging.getLogger("chat_insights.handoff")

# How far back from the claim an action unit may sit and still count as backing
# it. The observed shape is assistant/tool immediately before the claim, so 2
# covers it; anything further away belongs to an earlier turn.
_ACTION_LOOKBACK = 2

# Completed hand-off claims only. The auxiliary has to sit right against the
# participle, which is what separates a claim from a promise:
#
#   MATCHES     "I have forwarded your details and request to our sales team"
#               "I've forwarded your details to our sales team for a callback"
#               "your details have been passed to the team"
#   DOES NOT    "Once I have those, I'll make sure your request are forwarded"
#               "Once I have these details, I'll forward your request"
#
# That second group is the bot asking for more information. Treating it as a
# claim would flag most of the corpus and bury the real failures.
_CLAIM_PATTERNS = [
    # "I have forwarded", "we've now passed on", "I have already sent"
    re.compile(r"\b(?:i|we)\s*(?:'ve|’ve|\s+have)\s+"
                r"(?:now\s+|just\s+|already\s+|since\s+)?"
                r"(?:forwarded|passed|sent|submitted|shared|logged|registered)\b", re.I),
    # "your details have been forwarded", "the request has been passed on"
    re.compile(r"\b(?:details|request|enquiry|inquiry|information|info|message)\s+"
                r"(?:have|has)\s+been\s+"
                r"(?:forwarded|passed|sent|submitted|shared|logged|registered)\b", re.I),
]

# States, most urgent first. Ordering is meaningful: the report and the alert
# both sort on it.
CLAIMED_NOT_FIRED = "claimed_not_fired"
FIRED = "fired"
NO_CLAIM = "no_claim"

STATE_LABELS = {
    CLAIMED_NOT_FIRED: "Told the customer it was forwarded -- it was not",
    FIRED: "Forwarded to the sales team",
    NO_CLAIM: "Details shared, no hand-off attempted",
}

STATE_ORDER = {CLAIMED_NOT_FIRED: 0, NO_CLAIM: 1, FIRED: 2}


def _is_blank(message: dict) -> bool:
    return not (message.get("text") or "").strip()


def _role(message: dict) -> str:
    return (message.get("role") or "").lower()


def action_indices(messages: list[dict]) -> list[int]:
    """Positions of every action call in the conversation.

    Indexed by the tool message, since that is the half that unambiguously means
    "an action ran". The empty assistant message in front of it is accepted as
    the same evidence when a tool message is somehow absent -- if Chatbase ever
    stops reporting the tool role, detection degrades to the empty-assistant
    marker rather than silently deciding nothing ever fires.
    """
    out = []
    for i, message in enumerate(messages):
        if _role(message) == "tool":
            out.append(i)
        elif (_role(message) == "assistant" and _is_blank(message)
                and not any(_role(m) == "tool" for m in messages[i + 1:i + 2])):
            out.append(i)
    return out


def claim_indices(messages: list[dict]) -> list[int]:
    """Assistant messages that assert the hand-off already happened."""
    return [
        i for i, message in enumerate(messages)
        if _role(message) == "assistant"
        and any(p.search(message.get("text") or "") for p in _CLAIM_PATTERNS)
    ]


def claim_excerpt(text: str, limit: int = 220) -> str:
    """The sentence that made the claim, for quoting in the alert.

    Whoever picks this up needs to see the exact words the customer was told --
    "the bot said it was forwarded" is a different conversation to have with a
    customer than "the bot said someone would be in touch".
    """
    for pattern in _CLAIM_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        start = text.rfind(".", 0, match.start()) + 1
        end = text.find(".", match.end())
        end = len(text) if end == -1 else end + 1
        sentence = " ".join(text[start:end].split())
        return sentence[:limit]
    return ""


def inspect(conv: dict) -> dict:
    """What happened to this conversation's hand-off.

    Returns {state, claim_index, claim_excerpt, action_indices, detail}. `state`
    is NO_CLAIM when the bot never asserted a hand-off -- which is not itself a
    problem, but for a conversation carrying contact details it still means
    nobody was told to call anyone.
    """
    messages = conv.get("messages") or []
    actions = action_indices(messages)
    claims = claim_indices(messages)

    if not claims:
        return {
            "state": NO_CLAIM,
            "claim_index": None,
            "claim_excerpt": "",
            "action_indices": actions,
            "detail": ("The bot never said the details had been forwarded."
                        if not actions else
                        f"No hand-off claim. {len(actions)} action call(s) ran for something else."),
        }

    # The last claim is the one that counts. An earlier one can be backed by an
    # action for a different thing entirely, and it is the final promise the
    # customer is left holding.
    claim = claims[-1]
    backing = [a for a in actions if claim - _ACTION_LOOKBACK <= a < claim]
    excerpt = claim_excerpt(messages[claim].get("text") or "")

    if backing:
        return {
            "state": FIRED,
            "claim_index": claim,
            "claim_excerpt": excerpt,
            "action_indices": actions,
            "detail": f"An action ran at message {backing[-1]}, immediately before the claim.",
        }

    if actions:
        detail = (f"No action call in the {_ACTION_LOOKBACK} messages before the claim at "
                   f"{claim}. The conversation does contain action(s) at "
                   f"{', '.join(str(a) for a in actions)}, but for an earlier step.")
    else:
        detail = (f"The claim at message {claim} has no action call anywhere in the "
                   f"conversation -- nothing was sent.")
    return {
        "state": CLAIMED_NOT_FIRED,
        "claim_index": claim,
        "claim_excerpt": excerpt,
        "action_indices": actions,
        "detail": detail,
    }
