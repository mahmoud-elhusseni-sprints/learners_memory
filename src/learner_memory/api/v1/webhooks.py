"""Inbound LMS webhooks.

The LMS authenticates with the shared LC-API-KEY rather than one of our API keys,
and the webhook carries no organization, so both come from configuration
(LMS_API_KEY, LMS_ORGANIZATION_ID).
"""
from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from learner_memory.api.deps import SessionDep
from learner_memory.core.config import Settings, get_settings
from learner_memory.core.logging import get_logger
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.integrations.lms.client import API_KEY_HEADER
from learner_memory.integrations.lms.schemas import LmsLearnerEvent
from learner_memory.integrations.lms.syncs import sync_for
from learner_memory.schemas.webhook import WebhookAck, WebhookStatus
from learner_memory.workers.celery_app import celery_app
from learner_memory.workers.tasks.lms import SYNC_LEARNER_TASK

router = APIRouter(prefix="/webhooks/lms", tags=["webhooks"])
log = get_logger(__name__)

EVENT_ID_HEADER = "X-Learning-Companion-Event-Id"


def lms_organization(
    settings: Annotated[Settings, Depends(get_settings)],
    api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> uuid.UUID:
    """Authenticate the LMS and resolve the organization its learners belong to."""
    # The settings validator guarantees these are set together; checking both
    # keeps the types honest.
    secret, organization_id = settings.lms_api_key, settings.lms_organization_id
    if secret is None or organization_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "LMS webhooks are not enabled on this service")
    expected = secret.get_secret_value().encode()
    if api_key is None or not hmac.compare_digest(api_key.encode(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid {API_KEY_HEADER}")
    return organization_id


LmsOrgDep = Annotated[uuid.UUID, Depends(lms_organization)]


def lms_learner_repo(session: SessionDep, organization_id: LmsOrgDep) -> LearnerRepository:
    return LearnerRepository(session, organization_id)


@router.post("/learner", response_model=WebhookAck, status_code=status.HTTP_202_ACCEPTED)
async def receive_learner_event(
    event: LmsLearnerEvent,
    organization_id: LmsOrgDep,
    learners: Annotated[LearnerRepository, Depends(lms_learner_repo)],
    event_id_header: Annotated[uuid.UUID | None, Header(alias=EVENT_ID_HEADER)] = None,
) -> WebhookAck:
    """Accept a learner event and queue the sync its `api_url` includes call for;
    the LMS is read off the request path.

    404 when no learner in the organization holds `user_id` as its external id:
    the learner must be registered with that external id first. Events for data
    not synced yet are acknowledged as `ignored` so the LMS does not retry them.
    """
    if event_id_header is not None and event_id_header != event.event_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"{EVENT_ID_HEADER} does not match the body's event_id")

    sync = sync_for(event.includes())
    scope_id = sync.scope_id(event) if sync else None
    if sync and sync.scope_param and scope_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"{sync.kind} events must carry {sync.scope_param}")

    if await learners.get_by_external_id(event.user_id) is None:
        log.info("lms.webhook_unknown_learner", event_id=str(event.event_id),
                 external_id=event.user_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"learner with external_id {event.user_id} is not registered yet")

    if sync is None:
        log.info("lms.webhook_ignored", event_id=str(event.event_id),
                 includes=sorted(event.includes()))
        return WebhookAck(event_id=event.event_id, status=WebhookStatus.IGNORED)

    celery_app.send_task(SYNC_LEARNER_TASK, kwargs={
        "event_id": str(event.event_id),
        "external_id": event.user_id,
        "organization_id": str(organization_id),
        "kind": sync.kind.value,
        "scope_id": scope_id,
    })
    log.info("lms.webhook_queued", event_id=str(event.event_id), external_id=event.user_id,
             kind=sync.kind.value, scope_id=scope_id)
    return WebhookAck(event_id=event.event_id, status=WebhookStatus.QUEUED)
