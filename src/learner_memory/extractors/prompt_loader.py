"""Versioned prompt templates on disk: prompts/<source_type>/<version>.{system,user}.j2"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

PROMPT_ROOT = Path(__file__).parent / "prompts"


@dataclass(slots=True)
class RenderedPrompt:
    system: str
    user: str


class PromptLibrary:
    def __init__(self, root: Path = PROMPT_ROOT) -> None:
        self._env = Environment(
            loader=FileSystemLoader(root), undefined=StrictUndefined, autoescape=False
        )

    def render(self, *, source_type: str, version: str, context: dict) -> RenderedPrompt:
        def _one(kind: str) -> str:
            try:
                tpl = self._env.get_template(f"{source_type}/{version}.{kind}.j2")
            except TemplateNotFound:
                tpl = self._env.get_template(f"_shared/{version}.{kind}.j2")
            return tpl.render(**context)

        return RenderedPrompt(system=_one("system"), user=_one("user"))
