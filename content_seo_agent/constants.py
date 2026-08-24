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
    SMALL_MODEL = "small_model"
    CLAUDE = "claude"

    ALL = {SMALL_MODEL, CLAUDE}
