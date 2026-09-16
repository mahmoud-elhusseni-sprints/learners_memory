#!/usr/bin/env python
"""Issue, list and revoke API keys for remote services.

Keys are handed to services out of band, so a CLI is the whole surface — there is
no admin endpoint to secure. The plaintext key is printed once and never again;
only its SHA-256 lives in the database.

    python scripts/issue_api_key.py issue --org <uuid> --name coderbyte \
        --scopes profile:read --expires-days 365
    python scripts/issue_api_key.py list --org <uuid>
    python scripts/issue_api_key.py revoke --prefix a1b2c3d4e5f6
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from learner_memory.core.security import generate_api_key
from learner_memory.db.models.api_key import ApiKey
from learner_memory.db.session import unit_of_work


async def issue(org: uuid.UUID, name: str, scopes: list[str], expires_days: int | None) -> None:
    raw, prefix, key_hash = generate_api_key()
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None
    )
    async with unit_of_work() as session:
        session.add(ApiKey(
            organization_id=org,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            scopes=scopes,
            active=True,
            expires_at=expires_at,
        ))
    print(f"\n  service : {name}")
    print(f"  org     : {org}")
    print(f"  scopes  : {', '.join(scopes)}")
    print(f"  expires : {expires_at.isoformat() if expires_at else 'never'}")
    print(f"\n  API KEY : {raw}")
    print("\n  Copy it now — it is not recoverable.\n")


async def list_keys(org: uuid.UUID | None) -> None:
    async with unit_of_work() as session:
        stmt = select(ApiKey).order_by(ApiKey.created_at)
        if org:
            stmt = stmt.where(ApiKey.organization_id == org)
        for k in (await session.execute(stmt)).scalars():
            state = "revoked" if k.revoked_at else ("active" if k.active else "disabled")
            last = k.last_used_at.isoformat() if k.last_used_at else "never"
            print(f"{k.prefix}  {k.name:<24} {state:<9} scopes={','.join(k.scopes)} last_used={last}")


async def revoke(prefix: str) -> None:
    async with unit_of_work() as session:
        key = (
            await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        ).scalar_one_or_none()
        if key is None:
            raise SystemExit(f"no key with prefix {prefix}")
        key.active = False
        key.revoked_at = datetime.now(UTC)
    print(f"revoked {prefix}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue")
    p_issue.add_argument("--org", required=True, type=uuid.UUID)
    p_issue.add_argument("--name", required=True)
    p_issue.add_argument("--scopes", nargs="+", default=["profile:read"])
    p_issue.add_argument("--expires-days", type=int, default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("--org", type=uuid.UUID, default=None)

    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--prefix", required=True)

    args = parser.parse_args()
    if args.cmd == "issue":
        asyncio.run(issue(args.org, args.name, args.scopes, args.expires_days))
    elif args.cmd == "list":
        asyncio.run(list_keys(args.org))
    else:
        asyncio.run(revoke(args.prefix))


if __name__ == "__main__":
    main()
