#!/usr/bin/env python
"""Seed skill_catalog from docs/skills_taxonomy_framework.md (idempotent)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from learner_memory.core.config import get_settings
from learner_memory.core.taxonomy import Taxonomy
from learner_memory.db.models.learner import SkillCatalog
from learner_memory.db.session import unit_of_work


async def main() -> None:
    version = get_settings().taxonomy_version
    async with unit_of_work() as s:
        existing = {
            row.slug: row for row in (await s.execute(select(SkillCatalog))).scalars().all()
        }
        for skill in Taxonomy.all().values():
            row = existing.get(skill.slug)
            fields = dict(
                kind="general",
                category=skill.category,
                name=skill.name,
                level_descriptors={str(k): v for k, v in skill.level_descriptors.items()},
                best_evidence=list(skill.best_evidence),
                taxonomy_version=version,
                active=True,
            )
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
            else:
                s.add(SkillCatalog(slug=skill.slug, **fields))
    print(f"seeded {len(Taxonomy.all())} general skills (taxonomy_version={version})")


if __name__ == "__main__":
    asyncio.run(main())
