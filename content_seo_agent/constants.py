"""
Canonical vocabulary for the review queue -- status, task_type, and source
values. Every module that reads or writes review_queue rows should import
from here instead of using bare string literals, so the set of valid values
lives in exactly one place.
"""


class Status:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = {PENDING, APPROVED, REJECTED}


class TaskType:
    CLASSIFY = "classify"
    DRAFT = "draft"

    ALL = {CLASSIFY, DRAFT}


class Source:
    # Which model produced the row, i.e. how far up the escalation chain the
    # work had to travel: fine-tuned model -> general local model -> Claude.
    SMALL_MODEL = "small_model"
    FALLBACK_MODEL = "fallback_model"
    CLAUDE = "claude"

    ALL = {SMALL_MODEL, FALLBACK_MODEL, CLAUDE}

    LABELS = {
        SMALL_MODEL: "Local model",
        FALLBACK_MODEL: "Fallback model",
        CLAUDE: "Claude",
    }
