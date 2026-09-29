"""Rules shared by every sync that mirrors data owned by an external system."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum


class SyncOutcome(StrEnum):
    APPLIED = "applied"
    STALE = "stale"
    UNKNOWN_LEARNER = "unknown_learner"


def is_stale(applied_at: datetime | None, incoming_at: datetime | None) -> bool:
    """Incoming data is older than what is already applied.

    An equal time is not stale: the LMS stamps seconds, so two edits within one
    second share a timestamp. Data without a time on either side is applied.
    """
    if applied_at is None or incoming_at is None:
        return False
    return incoming_at < applied_at
