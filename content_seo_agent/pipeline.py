"""
Content/SEO Agent pipeline.

Flow per product:
  1. Classify, then 2. Draft, each run up the same escalation chain:

       fine-tuned small model  ->  general local model  ->  Claude

     A rung is only reached when the one before it produced low-confidence
     output (bad JSON, missing fields, or a safety flag on an unverified
     compliance claim). Both hops are configurable -- see the `escalation`
     block in config.yaml. With both off the chain is one rung long and
     low-confidence output still lands in the queue marked low, for a human.
     Nothing is dropped for want of a rung.
  3. Write result to the review queue (never writes to Odoo directly)

Run status (success/fail/escalated) is logged for every call so the
dashboard's "Agent Status" tab (Step 7 in the roadmap) has something
real to show, and so failures don't fail silently.
"""
import logging
import os

from config import settings
from config.settings import STANDARDS_WHITELIST
from content_seo_agent import small_model_client, claude_client, confidence, review_queue, title_parser
from content_seo_agent.constants import TaskType, Source

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "agent_runs.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger("pipeline")


def _log_run(product_id, stage, status, detail=""):
    logger.info("product_id=%s stage=%s status=%s detail=%s", product_id, stage, status, detail)


def _build_chain(small_call, fallback_call, claude_call) -> list:
    """The rungs to try, in order, given the current escalation settings.

    The fine-tuned model is always the first rung here -- skipping it is what
    approval_flow.regenerate_escalate_first does, and only for retries.
    """
    chain = [(Source.SMALL_MODEL, small_call)]
    if settings.ESCALATION_USE_FALLBACK_MODEL and settings.ESCALATION_FALLBACK_MODEL_NAME:
        chain.append((Source.FALLBACK_MODEL, fallback_call))
    if settings.ESCALATION_USE_CLAUDE:
        chain.append((Source.CLAUDE, claude_call))
    return chain


def _run_chain(product_id, stage: str, chain: list, score):
    """Walks the chain until a rung returns confident output.

    Returns (result, confidence, source, ok). When no rung succeeds it returns
    the LAST rung's output rather than the first: that came from the strongest
    model tried, so it is the better starting point for the human who now has
    to fix it. `ok` is False in that case and the caller decides what to log --
    a safety flag means something different from malformed JSON.
    """
    result, conf, source = {}, None, chain[0][0]
    for index, (source, call) in enumerate(chain):
        result = call()
        conf = score(result)
        if not conf.should_escalate:
            _log_run(product_id, stage,
                     "success_small_model" if index == 0
                     else "success_after_escalation_to_" + source)
            return result, conf, source, True
        if index + 1 < len(chain):
            _log_run(product_id, stage,
                     "escalating_" + source + "_to_" + chain[index + 1][0],
                     "; ".join(conf.reasons))
    return result, conf, source, False


def escalation_status() -> list:
    """(label, ok, detail) per rung, for the dashboard status panel and the
    CLI's --dry-run. Reports what the chain will actually do right now, which
    is not the same as what config asks for: a fallback model that is enabled
    but never pulled cannot run."""
    lines = [("Fine-tuned model",
              small_model_client.has_model(settings.SMALL_MODEL_NAME),
              settings.SMALL_MODEL_NAME + " via Ollama")]

    name = settings.ESCALATION_FALLBACK_MODEL_NAME
    if not settings.ESCALATION_USE_FALLBACK_MODEL:
        lines.append(("Fallback model", True, "disabled (escalation.use_fallback_model)"))
    elif not name:
        lines.append(("Fallback model", False,
                      "enabled but escalation.fallback_model_name is empty"))
    elif not small_model_client.has_model(name):
        lines.append(("Fallback model", False,
                      f"{name} is not pulled -- run: ollama pull {name}"))
    else:
        lines.append(("Fallback model", True, f"{name} via Ollama"))

    if not settings.ESCALATION_USE_CLAUDE:
        lines.append(("Claude escalation", True, "disabled (escalation.use_claude)"))
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        lines.append(("Claude escalation", False,
                      "enabled but ANTHROPIC_API_KEY is not set in .env"))
    else:
        lines.append(("Claude escalation", True,
                      f"{settings.CLAUDE_MODEL} via the Anthropic API"))
    return lines


def process_classification(product_id, title: str, description: str = "") -> dict:
    """Runs classification up the escalation chain, stopping at the first rung
    whose output scores as confident."""
    chain = _build_chain(
        lambda: small_model_client.classify(title, description),
        lambda: small_model_client.classify(title, description,
                                            model=settings.ESCALATION_FALLBACK_MODEL_NAME),
        lambda: claude_client.classify(title, description),
    )
    result, conf, source, ok = _run_chain(product_id, "classify", chain,
                                          confidence.score_classification)
    if not ok:
        _log_run(product_id, "classify", "failed_every_rung", "; ".join(conf.reasons))

    row = review_queue.add_to_queue(
        product_id=product_id,
        title=title,
        task_type=TaskType.CLASSIFY,
        source=source,
        parsed_output=result.get("parsed"),
        confidence=conf.confidence,
        reasons=conf.reasons,
        safety_flags=conf.safety_flags,
    )
    return row


def process_drafting(product_id, title: str) -> dict:
    """Runs drafting up the escalation chain -- a rung is only reached when the
    one before it came back low confidence or tripped the safety filter."""
    # Deterministic, code-only extraction -- never trust the model for these.
    title_facts = title_parser.parse_title(title)
    verified_claims = [STANDARDS_WHITELIST[product_id]] if product_id in STANDARDS_WHITELIST else []

    def score(result):
        return confidence.score_draft(result, title, verified_claims)

    chain = _build_chain(
        lambda: small_model_client.draft(title),
        lambda: small_model_client.draft(title, model=settings.ESCALATION_FALLBACK_MODEL_NAME),
        lambda: claude_client.draft(title),
    )
    result, conf, source, ok = _run_chain(product_id, "draft", chain, score)
    if not ok:
        if conf.safety_flags:
            # No model is trusted blindly on fabricated compliance claims --
            # the flag rides along to the queue and forces human attention
            # regardless of which rung produced the draft.
            _log_run(product_id, "draft", "safety_flag_after_escalation",
                     "; ".join(conf.safety_flags))
        else:
            _log_run(product_id, "draft", "failed_every_rung", "; ".join(conf.reasons))

    row = review_queue.add_to_queue(
        product_id=product_id,
        title=title,
        task_type=TaskType.DRAFT,
        source=source,
        parsed_output=result.get("parsed"),
        confidence=conf.confidence,
        reasons=conf.reasons,
        safety_flags=conf.safety_flags,
        is_regulated=title_facts["is_regulated"],
        ratio=title_facts["ratio"],
        quantity_detail=title_facts["quantity_detail"],
    )
    return row


def regenerate_draft_for_row(row_id: int) -> dict | None:
    """Generates a fresh draft for a row that was already reviewed (typically
    rejected) and puts it back in the queue.

    Without this a rejected product is stranded: its row sits in the queue
    forever, and because batch_runner dedups on "already has a draft row, any
    status", no future fetch will ever draft it again.

    Two things differ from a first-time draft, both aimed at not simply
    reproducing the draft a human already turned down:
      * the local model runs at a higher temperature (settings.REGENERATE_TEMPERATURE)
      * the reviewer's rejection reason is passed to the stronger model as
        grounding context when we escalate

    Set approval_flow.regenerate_escalate_first to skip the fine-tuned model
    entirely on retries -- if a person rejected its output once, a stronger
    rung is often the faster route to something publishable. If escalation is
    switched off entirely there is nothing stronger to skip to, so the
    fine-tuned model runs anyway, at the higher temperature.
    """
    row = review_queue.get_row(row_id)
    if not row:
        return None
    if row["task_type"] != TaskType.DRAFT:
        _log_run(row.get("product_id"), "regenerate", "skipped_not_a_draft")
        return row

    product_id = row["product_id"]
    title = row["title"]
    # The note from the rejection that is being retried. update_status stored
    # it on reviewer_note; apply_regenerated_draft moves it to
    # last_rejection_note, so on a second retry we read it from there.
    rejection_note = (row.get("reviewer_note") or row.get("last_rejection_note") or "").strip()

    title_facts = title_parser.parse_title(title)
    verified_claims = [STANDARDS_WHITELIST[product_id]] if product_id in STANDARDS_WHITELIST else []
    context = f"A reviewer rejected the previous draft for this reason: {rejection_note}" if rejection_note else ""

    def score(result):
        return confidence.score_draft(result, title, verified_claims)

    # The rejection note is grounding for every rung, not just Claude -- a
    # retry that stops at the fallback model should still know what the
    # reviewer objected to.
    chain = _build_chain(
        lambda: small_model_client.draft(title, temperature=settings.REGENERATE_TEMPERATURE,
                                         extra_context=context),
        lambda: small_model_client.draft(title, temperature=settings.REGENERATE_TEMPERATURE,
                                         model=settings.ESCALATION_FALLBACK_MODEL_NAME,
                                         extra_context=context),
        lambda: claude_client.draft(title, extra_context=context),
    )
    if settings.REGENERATE_ESCALATE_FIRST and len(chain) > 1:
        # Straight past the model whose output was already rejected once.
        chain = chain[1:]
        _log_run(product_id, "regenerate", "escalated_first_to_" + chain[0][0], rejection_note)

    result, conf, source, ok = _run_chain(product_id, "regenerate", chain, score)
    if not ok:
        _log_run(product_id, "regenerate", "failed_every_rung", "; ".join(conf.reasons))

    return review_queue.apply_regenerated_draft(
        row_id,
        parsed_output=result.get("parsed"),
        source=source,
        confidence=conf.confidence,
        reasons=conf.reasons,
        safety_flags=conf.safety_flags,
    )


def process_product(product_id, title: str, description: str = "") -> dict:
    """Convenience entry point: runs both classify and draft for one product."""
    if not small_model_client.is_available():
        _log_run(product_id, "startup", "small_model_unavailable",
                  "Ollama not reachable at 127.0.0.1:11434 -- falling through to Claude only")

    classify_row = process_classification(product_id, title, description)
    draft_row = process_drafting(product_id, title)
    return {"classify": classify_row, "draft": draft_row}
