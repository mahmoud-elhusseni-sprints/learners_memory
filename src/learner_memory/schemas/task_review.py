"""The task-review envelope: a learner's task submission plus the grader's report.

An upstream learn-os service posts us one document per reviewed submission — the
task that was set, the learner's own work, and the grader's written report on it.
Like the coderbyte contract, the model is deliberately tolerant: every field is
optional and the common key spellings are accepted as aliases, because the
producer is outside our release cycle.

The reviewer's own identity (name/email) is intentionally absent from the rendered
text — the learner is identified by `learner_id` on the ingest envelope, and card
content must stay free of identifying details.

There is no score, verdict or rubric here by design: the review is a narrative
report only, so the extraction reads the grader's actual observations rather than
a number.
"""
from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

_SECTIONS = ("task", "submission", "review")


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class Deliverables(BaseModel):
    """Deliverable expectations for the task."""

    format: str | None = None
    max_pages: int | None = None
    required_sections: list[str] = Field(default_factory=list)
    expected_effort_hours: float | None = None
    expected_repo_paths: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("required_sections", "expected_repo_paths", mode="before")
    @classmethod
    def _list_fields(cls, value: Any) -> list[Any]:
        return _coerce_list(value)


class Dependencies(BaseModel):
    """Dependencies and integration notes for the task."""

    depends_on_previous_tasks: list[int | str] = Field(default_factory=list)
    integration_notes: str | None = None

    model_config = ConfigDict(extra="ignore")

    @field_validator("depends_on_previous_tasks", mode="before")
    @classmethod
    def _list_fields(cls, value: Any) -> list[Any]:
        return _coerce_list(value)


class TaskReview(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # the task that was set
    project: str | None = None
    headline: str | None = Field(
        None, validation_alias=AliasChoices("headline", "title", "name", "task_title")
    )
    task_number: int | None = None
    workstream: str | None = None
    description: str | None = Field(
        None, validation_alias=AliasChoices("description", "prompt", "brief", "instructions")
    )
    assets: list[dict[str, Any]] = Field(default_factory=list)
    deliverables: Deliverables | None = None
    technologies: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("technologies", "topics", "tags", "skills")
    )
    functional_requirements: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    dependencies: Dependencies | None = None
    review_notes: list[str] = Field(default_factory=list)

    # the learner's submission — a list of URLs (repo, PR, deployed app, ...)
    submission: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "submission", "submission_urls", "urls", "links", "repo_urls"
        ),
    )

    # the grader's report — a narrative only, no score/verdict/rubric
    report: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "report", "review_report", "grader_report", "feedback", "comment", "notes"
        ),
    )
    reviewer_id: str | None = Field(
        None, validation_alias=AliasChoices("reviewer_id", "grader_id", "reviewer")
    )
    iteration: int | None = Field(
        None, validation_alias=AliasChoices("iteration", "attempt", "revision")
    )

    @field_validator(
        "assets", "technologies", "functional_requirements", "non_functional_requirements",
        "acceptance_checks", "definition_of_done", "review_notes", "submission",
        mode="before",
    )
    @classmethod
    def _list_fields(cls, value: Any) -> list[Any]:
        return _coerce_list(value)

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> TaskReview:
        """Accepts the document flat, or split into `task`/`submission`/`review`
        objects. Nested sections are flattened before validation; a key present at
        the top level wins over the same key inside a section. `submission` is a
        special case: a section that is a list (or a bare string) is treated as the
        list of submission URLs directly, rather than a dict of fields."""
        merged: dict[str, Any] = {}
        for section in _SECTIONS:
            value = doc.get(section)
            if isinstance(value, dict):
                merged.update(value)
            elif section == "submission" and value is not None:
                merged["submission"] = value
        merged.update({k: v for k, v in doc.items() if k not in _SECTIONS})
        return cls.model_validate(merged)

    def submission_or_raise(self) -> list[str]:
        if not self.submission:
            raise ValueError(
                "task_review payload contains no submission; refusing to extract from an "
                "empty submission (check the producer's field names)"
            )
        return self.submission

    def to_text(self) -> str:
        """One self-contained, greppable block: the task, then the work, then the
        grader's report."""
        lines = [f"Task: {self.headline or 'Untitled task'}"]
        if self.workstream:
            lines.append(f"Workstream: {self.workstream}")
        if self.technologies:
            lines.append(f"Technologies: {', '.join(self.technologies)}")
        if self.iteration is not None:
            lines.append(f"Iteration: {self.iteration}")
        if self.description:
            lines.append(f"Task description:\n{self.description.strip()}")
        if self.functional_requirements:
            lines.append("Functional requirements: " + "; ".join(self.functional_requirements))
        if self.non_functional_requirements:
            lines.append(
                "Non-functional requirements: " + "; ".join(self.non_functional_requirements)
            )
        if self.acceptance_checks:
            lines.append("Acceptance checks: " + "; ".join(self.acceptance_checks))
        if self.definition_of_done:
            lines.append("Definition of done: " + "; ".join(self.definition_of_done))

        lines.append(
            "Learner's submission URLs:\n" + "\n".join(f"- {url}" for url in self.submission)
        )
        if self.report:
            lines.append(f"Grader's report:\n{self.report.strip()}")
        return "\n".join(lines)
