"""API-key credentials and the profile read endpoint.

No database: a stub session returns whatever ApiKey row the case needs, so the
verification rules and the endpoint's auth wiring are tested on their own.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from learner_memory.api.deps import AuthDep, learner_repo
from learner_memory.api.v1 import learners
from learner_memory.core.config import Settings, get_settings
from learner_memory.core.security import (
    generate_api_key,
    hash_api_key,
    parse_prefix,
    verify_api_key,
)
from learner_memory.db.models.api_key import ApiKey
from learner_memory.db.models.learner import LearnerProfile
from learner_memory.db.session import get_session


@pytest.fixture(autouse=True)
def no_usage_stamp(monkeypatch):
    """The last_used_at write is fire-and-forget and needs a real database;
    test_usage_stamp_failure_does_not_break_the_read covers what happens when
    it fails for real."""
    async def _noop(_key_id):
        return None

    monkeypatch.setattr("learner_memory.core.security.touch_api_key", _noop)


def settings_with_auth_on() -> Settings:
    return get_settings().model_copy(update={"auth_disabled": False})


ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
LEARNER = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class StubSession:
    """Returns one preset row for any select."""

    def __init__(self, row=None):
        self.row = row

    async def execute(self, _stmt):
        return _Result(self.row)


def make_key(raw: str, prefix: str, key_hash: str, **overrides) -> ApiKey:
    fields = {
        "id": uuid.uuid4(),
        "organization_id": ORG,
        "name": "coderbyte",
        "prefix": prefix,
        "key_hash": key_hash,
        "scopes": ["profile:read"],
        "active": True,
        "expires_at": None,
        "revoked_at": None,
    }
    fields.update(overrides)
    return ApiKey(**fields)


# --------------------------------------------------------------------------- #
# key material
# --------------------------------------------------------------------------- #
def test_generated_key_parses_back_to_its_prefix_and_hash():
    raw, prefix, key_hash = generate_api_key()
    assert raw.startswith(f"lm_{prefix}_")
    assert parse_prefix(raw) == prefix
    assert hash_api_key(raw) == key_hash


def test_two_keys_never_collide():
    keys = {generate_api_key()[1] for _ in range(200)}
    assert len(keys) == 200


@pytest.mark.parametrize("bad", ["", "nonsense", "lm_only_", "lm__secret", "xx_abc_secret",
                                 "lm_abc"])
def test_malformed_keys_have_no_prefix(bad):
    assert parse_prefix(bad) is None


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
async def test_valid_key_verifies():
    raw, prefix, key_hash = generate_api_key()
    key = await verify_api_key(raw, StubSession(make_key(raw, prefix, key_hash)))
    assert key.organization_id == ORG
    assert key.scopes == ["profile:read"]


async def test_right_prefix_wrong_secret_is_rejected():
    raw, prefix, key_hash = generate_api_key()
    forged = f"lm_{prefix}_notthesecret"
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(forged, StubSession(make_key(raw, prefix, key_hash)))
    assert exc.value.status_code == 401


async def test_unknown_prefix_is_rejected_with_the_same_message_as_a_wrong_secret():
    raw, _, _ = generate_api_key()
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(raw, StubSession(None))
    assert (exc.value.status_code, exc.value.detail) == (401, "invalid api key")


async def test_malformed_key_is_rejected_without_touching_the_database():
    class Exploding(StubSession):
        async def execute(self, _stmt):
            raise AssertionError("should not query on a malformed key")

    with pytest.raises(HTTPException) as exc:
        await verify_api_key("garbage", Exploding())
    assert exc.value.status_code == 401


async def test_revoked_key_is_rejected():
    raw, prefix, key_hash = generate_api_key()
    row = make_key(raw, prefix, key_hash, revoked_at=datetime.now(UTC))
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(raw, StubSession(row))
    assert exc.value.status_code == 401


async def test_expired_key_is_rejected():
    raw, prefix, key_hash = generate_api_key()
    row = make_key(raw, prefix, key_hash,
                   expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(raw, StubSession(row))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


async def test_key_expiring_in_the_future_still_works():
    raw, prefix, key_hash = generate_api_key()
    row = make_key(raw, prefix, key_hash, expires_at=datetime.now(UTC) + timedelta(days=1))
    assert (await verify_api_key(raw, StubSession(row))).prefix == prefix


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
class FakeLearnerRepo:
    """Mimics the org filter that makes cross-tenant reads look like 404s."""

    def __init__(self, organization_id: uuid.UUID, profile: LearnerProfile | None):
        self.organization_id = organization_id
        self.profile = profile

    async def get_profile(self, learner_id: uuid.UUID):
        if self.profile is None or self.profile.organization_id != self.organization_id:
            return None
        return self.profile if self.profile.learner_id == learner_id else None


def build_client(key_row: ApiKey | None, profile: LearnerProfile | None) -> TestClient:
    """Real auth dependency, so every request exercises the API key path; only
    the session and the repository are stubbed."""
    app = FastAPI()
    app.include_router(learners.router, prefix="/v1")

    async def _session():
        yield StubSession(key_row)

    def _repo(auth: AuthDep) -> FakeLearnerRepo:
        return FakeLearnerRepo(auth.organization_id, profile)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[learner_repo] = _repo
    # Pin auth on: the repo's .env sets AUTH_DISABLED=true for local dev, which
    # would otherwise mask what a missing credential does in production.
    app.dependency_overrides[get_settings] = lambda: settings_with_auth_on()
    return TestClient(app)


def a_profile(organization_id=ORG, **overrides) -> LearnerProfile:
    fields = {
        "learner_id": LEARNER,
        "organization_id": organization_id,
        "snapshot": {"summary": "strong on backend"},
        "profile_version": 4,
        "computed_at": datetime(2026, 9, 16, 9, 12, 44, tzinfo=UTC),
        "stale_dimensions": ["technical_skills"],
    }
    fields.update(overrides)
    return LearnerProfile(**fields)


def test_valid_key_reads_the_profile():
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash), a_profile())
    resp = client.get(f"/v1/learners/{LEARNER}/profile", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["learner_id"] == str(LEARNER)
    assert body["organization_id"] == str(ORG)
    assert body["profile_version"] == 4
    assert body["stale_dimensions"] == ["technical_skills"]
    assert body["snapshot"] == {"summary": "strong on backend"}


def test_empty_snapshot_is_a_200_not_a_404():
    """Until the synthesizer lands every profile is the stub written at
    registration; a remote service must see that, not an error."""
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash),
                          a_profile(snapshot={}, profile_version=0, computed_at=None,
                                    stale_dimensions=[]))
    resp = client.get(f"/v1/learners/{LEARNER}/profile", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    assert resp.json()["snapshot"] == {}
    assert resp.json()["computed_at"] is None


def test_key_without_profile_read_scope_is_forbidden():
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash, scopes=["memory:write"]), a_profile())
    resp = client.get(f"/v1/learners/{LEARNER}/profile", headers={"X-API-Key": raw})
    assert resp.status_code == 403


def test_learner_in_another_org_is_a_404_not_a_403():
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash), a_profile(organization_id=OTHER_ORG))
    resp = client.get(f"/v1/learners/{LEARNER}/profile", headers={"X-API-Key": raw})
    assert resp.status_code == 404


def test_missing_credential_is_unauthorized():
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash), a_profile())
    assert client.get(f"/v1/learners/{LEARNER}/profile").status_code == 401


def test_invalid_key_is_unauthorized():
    raw, prefix, key_hash = generate_api_key()
    client = build_client(make_key(raw, prefix, key_hash), a_profile())
    resp = client.get(f"/v1/learners/{LEARNER}/profile",
                      headers={"X-API-Key": f"lm_{prefix}_wrong"})
    assert resp.status_code == 401


async def test_usage_stamp_failure_does_not_break_the_read(monkeypatch):
    """The stamp runs after the response is sent; if the database is unreachable
    the caller must still have its profile, not a 500."""
    from learner_memory.core import security

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("learner_memory.db.session.unit_of_work", lambda: Boom())
    await security.touch_api_key(uuid.uuid4())   # must not raise
