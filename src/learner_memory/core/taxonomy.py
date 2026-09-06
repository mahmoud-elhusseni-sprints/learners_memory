"""The general-skills taxonomy: 34 subskills x 6 levels.

`docs/skills_taxonomy_framework.md` stays the human-editable source of truth;
this module parses it once and freezes it. Nothing downstream may invent a skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learner_memory.schemas.memory_card import Contribution

TAXONOMY_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "skills_taxonomy_framework.md"
)
LEVEL_LABELS = {
    1: "Awareness", 2: "Understanding", 3: "Applied",
    4: "Adaptive", 5: "Advanced", 6: "Strategic",
}


@dataclass(frozen=True, slots=True)
class Skill:
    slug: str
    name: str
    category: str
    level_descriptors: dict[int, str]
    best_evidence: tuple[str, ...]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


@lru_cache
def _load(path: str | None = None) -> dict[str, Skill]:
    text = Path(path or TAXONOMY_PATH).read_text(encoding="utf-8")
    skills: dict[str, Skill] = {}
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or cells[0] in ("Category", "") or set(cells[1]) <= {":", "-"}:
            continue
        slug = _slugify(cells[1])
        skills[slug] = Skill(
            slug=slug,
            name=cells[1],
            category=cells[0],
            level_descriptors={i: cells[1 + i] for i in range(1, 7)},
            best_evidence=tuple(e.strip() for e in cells[8].split(",") if e.strip()),
        )
    return skills


class Taxonomy:
    """Static facade over the frozen skill set."""

    @staticmethod
    def all() -> dict[str, Skill]:
        return _load()

    @staticmethod
    def slugs() -> frozenset[str]:
        return frozenset(_load())

    @staticmethod
    def get(slug: str) -> Skill | None:
        return _load().get(slug)

    @staticmethod
    def is_valid(slug: str) -> bool:
        return slug in _load()

    @staticmethod
    def filter_contributions(
        contributions: list["Contribution"],
    ) -> tuple[list["Contribution"], list["Contribution"]]:
        """Split into (kept, dropped). Only `general_skill` keys are constrained."""
        kept, dropped = [], []
        for c in contributions:
            if c.target == "general_skill" and not Taxonomy.is_valid(c.key):
                dropped.append(c)
            else:
                kept.append(c)
        return kept, dropped

    @staticmethod
    def prompt_block() -> str:
        """Compact taxonomy rendering injected into extraction prompts."""
        lines = []
        for skill in _load().values():
            levels = " | ".join(
                f"L{i} {LEVEL_LABELS[i]}: {skill.level_descriptors[i]}" for i in range(1, 7)
            )
            lines.append(f"- {skill.slug} ({skill.category} / {skill.name}): {levels}")
        return "\n".join(lines)
