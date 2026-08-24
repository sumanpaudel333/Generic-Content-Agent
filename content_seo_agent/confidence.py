"""
Decides whether the small model's output is trustworthy enough to go
straight to the review queue, or whether it needs escalation to Claude.

This is deliberately conservative. Every draft still goes to human
review regardless of source (draft-only guardrail is non-negotiable),
but confidence determines whether Claude gets a shot at a better
draft BEFORE a human ever sees it, and whether flags get attached
that a reviewer should pay extra attention to.
"""
from dataclasses import dataclass, field

from config import settings
from content_seo_agent.content_status import NEEDS_DRAFTING, NEEDS_DISCLAIMER_ONLY
from content_seo_agent.safety_filter import scan_draft_json

REQUIRED_DRAFT_FIELDS = ["overview", "features", "applications"]
REQUIRED_CLASSIFY_FIELDS = ["is_regulated", "content_status", "product_type"]

# Reuses the same status set content_status.py's deterministic classifier
# produces, so this validation can never drift out of sync with it.
VALID_CONTENT_STATUS = NEEDS_DRAFTING | NEEDS_DISCLAIMER_ONLY | {"good"}
VALID_PRODUCT_TYPE = {"bulk_bag", "roll", "bagged", "each_or_pack", "other"}


@dataclass
class ConfidenceResult:
    confidence: str  # "high" or "low"
    reasons: list[str] = field(default_factory=list)
    safety_flags: list[str] = field(default_factory=list)
    should_escalate: bool = False


def score_classification(result: dict) -> ConfidenceResult:
    reasons = []

    if not result.get("parse_success"):
        return ConfidenceResult("low", ["JSON did not parse"], [], should_escalate=True)

    parsed = result.get("parsed") or {}
    missing_fields = [f for f in REQUIRED_CLASSIFY_FIELDS if f not in parsed]
    if missing_fields:
        reasons.append(f"missing fields: {missing_fields}")

    if parsed.get("content_status") not in VALID_CONTENT_STATUS:
        reasons.append(f"invalid content_status: {parsed.get('content_status')!r}")

    if parsed.get("product_type") not in VALID_PRODUCT_TYPE:
        reasons.append(f"invalid product_type: {parsed.get('product_type')!r}")

    if not isinstance(parsed.get("is_regulated"), bool):
        reasons.append("is_regulated not a boolean")

    should_escalate = len(reasons) > 0
    confidence = "low" if should_escalate else "high"
    return ConfidenceResult(confidence, reasons, [], should_escalate)


def score_draft(result: dict, title: str = "", verified_claims: list[str] | None = None) -> ConfidenceResult:
    reasons = []

    if not result.get("parse_success"):
        return ConfidenceResult("low", ["JSON did not parse"], [], should_escalate=True)

    parsed = result.get("parsed") or {}
    missing_fields = [f for f in REQUIRED_DRAFT_FIELDS if f not in parsed]
    if missing_fields:
        reasons.append(f"missing fields: {missing_fields}")

    overview = parsed.get("overview", "")
    if not overview or len(overview) < settings.MIN_OVERVIEW_LENGTH:
        reasons.append("overview missing or too short")

    features = parsed.get("features", [])
    if not isinstance(features, list) or len(features) == 0:
        reasons.append("features missing or empty")

    # This is the check that catches fabrications like an unverified standards claim
    # compliance claim and the "roll of 2000" -> "20,000" quantity error.
    safety_flags = scan_draft_json(parsed, title, verified_claims)
    if safety_flags:
        reasons.append(f"unverified claim(s) needing review: {safety_flags}")

    should_escalate = len(reasons) > 0
    confidence = "low" if should_escalate else "high"
    return ConfidenceResult(confidence, reasons, safety_flags, should_escalate)
