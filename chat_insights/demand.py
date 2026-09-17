"""
What customers are asking about, in a form the content agent can order by.

The two automations have never spoken to each other, and the gap costs real
work. Chat Insights knows which products customers ask about and which of those
the bot could not answer. The content agent drafts product descriptions ten a
day out of a backlog, in whatever order the export file happens to list them
(`target_df.head(limit)` in batch_runner). So a product nobody has mentioned in
months gets written before one three customers asked about last week.

This closes that loop. It turns the stored weekly keywords into a weight per
term, and the content agent sorts its queue by how well a product title matches.

Two properties matter:

  It only ever REORDERS. Nothing is added to or removed from the backlog, and
  every product still gets drafted -- the ones customers are asking about
  simply get drafted first. That keeps this safe to switch on: the worst case
  is the order you already had.

  It cannot break the content job. The content agent does not import this
  module; its caller passes the terms in. If Chat Insights is not configured,
  has never run, or throws, the caller passes nothing and the order is
  unchanged.

Weighting: a term the bot FAILED on counts for more than one it answered. Both
are demand, but a question we could not answer is a page that is missing, which
is exactly what the content agent exists to write.
"""
import logging
import re

from chat_insights import db

logger = logging.getLogger("chat_insights.demand")

# How much more an unanswered mention is worth than an answered one. A term the
# bot handled fine is still demand -- the page is working, so writing it again
# is not urgent.
UNANSWERED_WEIGHT = 3
ANSWERED_WEIGHT = 1

# Terms too generic to steer an ordering. "delivery" is the most-asked term in
# the corpus and matches nothing in a product title; "bag" matches half the
# catalogue. Both would flatten the ranking rather than sharpen it.
_TOO_GENERIC = frozenset("""
delivery deliver price pricing cost quote quotation order bag bags bulk
tonne tonnes cubic metre meter litre size sizes colour color available
stock delivery-fee product products item items
""".split())

_MIN_TERM_LENGTH = 3


def terms_from_runs(limit_runs: int = 4) -> dict[str, int]:
    """Demand weights per term, from the most recent completed weekly runs.

    Reads the keywords already stored on each run rather than re-analysing:
    they were computed when the week ran, they are per-conversation counts
    (so one chatty customer cannot skew them), and they already carry how many
    of those conversations the bot failed on.
    """
    weights: dict[str, int] = {}
    runs = [r for r in db.list_runs(limit=limit_runs * 3)
            if r.get("status") == db.STATUS_COMPLETE and (r.get("stats") or {}).get("keywords")]
    for run in runs[:limit_runs]:
        for entry in run["stats"]["keywords"]:
            term = str(entry.get("term") or "").strip().lower()
            if len(term) < _MIN_TERM_LENGTH:
                continue
            if all(part in _TOO_GENERIC for part in term.split()):
                continue
            answered = max(int(entry.get("conversations") or 0)
                            - int(entry.get("unanswered") or 0), 0)
            unanswered = int(entry.get("unanswered") or 0)
            weights[term] = (weights.get(term, 0)
                             + unanswered * UNANSWERED_WEIGHT
                             + answered * ANSWERED_WEIGHT)
    return weights


_WORD_RE = re.compile(r"[a-z0-9]+")


def score_title(title: str, weights: dict[str, int]) -> int:
    """How much customer demand a product title matches.

    Matching is on whole words, not substrings: "sand" must not score
    "Sandstone", and "mix" must not score "Mixed". A multi-word term has to
    appear as a phrase.
    """
    if not title or not weights:
        return 0
    words = _WORD_RE.findall(title.lower())
    if not words:
        return 0
    word_set = set(words)
    joined = " ".join(words)
    score = 0
    for term, weight in weights.items():
        if " " in term:
            if term in joined:
                score += weight
        elif term in word_set:
            score += weight
    return score


def explain(title: str, weights: dict[str, int]) -> list[str]:
    """Which demand terms a title matched, for logging why it was prioritised."""
    if not title or not weights:
        return []
    words = _WORD_RE.findall(title.lower())
    word_set = set(words)
    joined = " ".join(words)
    matched = [t for t in weights
               if (t in joined) if " " in t] + [t for t in weights
                                                 if " " not in t and t in word_set]
    return sorted(set(matched), key=lambda t: -weights[t])


def current_terms(limit_runs: int = 4) -> dict[str, int]:
    """Demand weights, or {} if anything at all goes wrong.

    Never raises. The content agent's daily job calls this, and a chat-side
    problem must not stop product descriptions being drafted.
    """
    try:
        weights = terms_from_runs(limit_runs)
        logger.info("Demand terms from the last %s run(s): %s", limit_runs, len(weights))
        return weights
    except Exception:
        logger.exception("Could not read demand terms; drafting order is unchanged")
        return {}
