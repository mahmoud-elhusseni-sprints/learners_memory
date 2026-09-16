"""Request-scoped dependencies: auth context, session, repositories."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from learner_memory.core.config import Settings, get_settings
from learner_memory.db.repositories.learner import LearnerRepository
from learner_memory.db.session import get_session


@dataclass(slots=True)
class AuthContext:
    organization_id: uuid.UUID
    subject: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "admin" not in self.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing scope '{scope}'")


async def get_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    background: BackgroundTasks,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_organization_id: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """org_id always comes from the credential — never from a request body.

    An API key identifies a remote *service* and is checked before anything else,
    so a key keeps working in local dev where `auth_disabled` short-circuits JWTs.
    """
    if x_api_key:
        from learner_memory.core.security import touch_api_key, verify_api_key

        key = await verify_api_key(x_api_key, session)
        # Off the hot path: the usage stamp runs after the response is sent, in
        # its own transaction, so a read never turns into a write.
        background.add_task(touch_api_key, key.id)
        return AuthContext(
            organization_id=key.organization_id,
            subject=f"apikey:{key.name}",
            scopes=frozenset(key.scopes or ()),
        )

    if settings.auth_disabled:
        if not x_organization_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Organization-Id required")
        return AuthContext(uuid.UUID(x_organization_id), "local-dev", frozenset({"admin"}))

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
    from learner_memory.core.security import verify_token

    claims = await verify_token(authorization.split(" ", 1)[1], settings)
    return AuthContext(
        organization_id=uuid.UUID(claims["organization_id"]),
        subject=claims.get("sub", "unknown"),
        scopes=frozenset(claims.get("scopes", [])),
    )


SessionDep = Annotated[AsyncSession, Depends(get_session)]
AuthDep = Annotated[AuthContext, Depends(get_auth)]


def learner_repo(session: SessionDep, auth: AuthDep) -> LearnerRepository:
    return LearnerRepository(session, auth.organization_id)


LearnerRepoDep = Annotated[LearnerRepository, Depends(learner_repo)]
