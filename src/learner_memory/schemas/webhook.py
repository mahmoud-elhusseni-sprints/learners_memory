from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel


class WebhookStatus(StrEnum):
    QUEUED = "queued"      # accepted; the sync runs in the background
    IGNORED = "ignored"    # valid, but for data this service does not sync yet


class WebhookAck(BaseModel):
    event_id: uuid.UUID
    status: WebhookStatus
