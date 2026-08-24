"""
Content/SEO Agent pipeline.

Flow per product:
  1. Classify (small model first, escalate to Claude if low confidence)
  2. Draft (small model first, escalate to Claude if low confidence
     or the safety filter flags an unverified compliance claim)
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


def process_classification(product_id, title: str, description: str = "") -> dict:
    """Runs classification with small-model-first, Claude-escalation-on-low-confidence."""
    result = small_model_client.classify(title, description)
    conf = confidence.score_classification(result)
    source = Source.SMALL_MODEL

    if conf.should_escalate:
        _log_run(product_id, "classify", "escalating", "; ".join(conf.reasons))
        result = claude_client.classify(title, description)
        conf = confidence.score_classification(result)
        source = Source.CLAUDE
        if conf.should_escalate:
            _log_run(product_id, "classify", "failed_both", "; ".join(conf.reasons))
        else:
            _log_run(product_id, "classify", "success_after_escalation")
    else:
        _log_run(product_id, "classify", "success_small_model")

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
    """Runs drafting with small-model-first, Claude-escalation on low confidence or safety flag."""
    # Deterministic, code-only extraction -- never trust the model for these.
    title_facts = title_parser.parse_title(title)
    verified_claims = [STANDARDS_WHITELIST[product_id]] if product_id in STANDARDS_WHITELIST else []

    result = small_model_client.draft(title)
    conf = confidence.score_draft(result, title, verified_claims)
    source = Source.SMALL_MODEL

    if conf.should_escalate:
        _log_run(product_id, "draft", "escalating", "; ".join(conf.reasons))
        result = claude_client.draft(title)
        conf = confidence.score_draft(result, title, verified_claims)
        source = Source.CLAUDE
        if conf.safety_flags:
            # Even Claude isn't trusted blindly on fabricated compliance
            # claims -- this always forces human attention regardless
            # of which model produced it.
            _log_run(product_id, "draft", "safety_flag_after_escalation", "; ".join(conf.safety_flags))
        if conf.should_escalate and not conf.safety_flags:
            _log_run(product_id, "draft", "failed_both", "; ".join(conf.reasons))
        else:
            _log_run(product_id, "draft", "success_after_escalation")
    else:
        _log_run(product_id, "draft", "success_small_model")

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

    Set approval_flow.regenerate_escalate_first to skip the local model
    entirely on retries -- if a person rejected its output once, the stronger
    model is often the faster route to something publishable.
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

    if settings.REGENERATE_ESCALATE_FIRST:
        result = claude_client.draft(title, extra_context=context)
        conf = confidence.score_draft(result, title, verified_claims)
        source = Source.CLAUDE
        _log_run(product_id, "regenerate", "escalated_first", rejection_note)
    else:
        result = small_model_client.draft(title, temperature=settings.REGENERATE_TEMPERATURE)
        conf = confidence.score_draft(result, title, verified_claims)
        source = Source.SMALL_MODEL
        if conf.should_escalate:
            _log_run(product_id, "regenerate", "escalating", "; ".join(conf.reasons))
            result = claude_client.draft(title, extra_context=context)
            conf = confidence.score_draft(result, title, verified_claims)
            source = Source.CLAUDE
        else:
            _log_run(product_id, "regenerate", "success_small_model")

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
