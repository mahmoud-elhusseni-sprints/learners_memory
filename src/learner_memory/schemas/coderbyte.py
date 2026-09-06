"""The Coderbyte assessment envelope, as delivered by the learn-os reader service.

We never talk to Coderbyte. An upstream learn-os service polls it and posts us a
JSON document; these models are the contract for that document.

The models are deliberately tolerant: every field is optional and the common
key spellings are accepted as aliases, because the reader service is outside our
release cycle and a renamed field must not cost us a whole assessment. What we
cannot tolerate silently is a *missing question list* — that is a real failure,
raised by `CoderbyteAssessment.questions_or_raise`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class TestCaseResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    passed: int | None = None
    total: int | None = None

    @property
    def summary(self) -> str | None:
        if self.passed is None or self.total is None:
            return None
        return f"{self.passed}/{self.total} test cases passed"


class CoderbyteQuestion(BaseModel):
    """One question and what the learner did with it."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = Field(None, validation_alias=AliasChoices("id", "question_id", "uuid"))
    kind: str | None = Field(
        None, validation_alias=AliasChoices("kind", "type", "question_type", "category")
    )
    title: str | None = Field(None, validation_alias=AliasChoices("title", "name"))
    prompt: str | None = Field(
        None, validation_alias=AliasChoices("prompt", "text", "body", "description", "question")
    )
    language: str | None = Field(None, validation_alias=AliasChoices("language", "lang"))
    topics: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("topics", "tags", "skills")
    )

    answer: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "answer", "response", "submission", "candidate_answer", "solution", "code"
        ),
    )
    expected_answer: str | None = Field(
        None, validation_alias=AliasChoices("expected_answer", "correct_answer", "expected")
    )
    is_correct: bool | None = Field(
        None, validation_alias=AliasChoices("is_correct", "correct", "passed")
    )
    score: float | None = Field(None, validation_alias=AliasChoices("score", "points", "earned"))
    max_score: float | None = Field(
        None, validation_alias=AliasChoices("max_score", "max_points", "total_points", "out_of")
    )
    time_spent_seconds: float | None = Field(
        None,
        validation_alias=AliasChoices("time_spent_seconds", "time_spent", "duration_seconds"),
    )
    attempts: int | None = Field(None, validation_alias=AliasChoices("attempts", "tries"))
    test_cases: TestCaseResult | None = Field(
        None, validation_alias=AliasChoices("test_cases", "tests", "test_results")
    )

    def score_line(self) -> str | None:
        if self.score is None:
            return None
        return f"{self.score:g}/{self.max_score:g}" if self.max_score is not None else f"{self.score:g}"


class CoderbyteAssessment(BaseModel):
    """The whole delivered document.

    Candidate identity is intentionally absent. The reader service may include it;
    we drop it here so it can never reach a prompt, a card or an embedding — memory
    card `content` is required to be free of identifying details, and the learner is
    already identified by `learner_id` on the ingest envelope.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    assessment_id: str | None = Field(
        None, validation_alias=AliasChoices("assessment_id", "id", "uuid")
    )
    name: str | None = Field(
        None, validation_alias=AliasChoices("name", "title", "assessment_name")
    )
    score: float | None = Field(None, validation_alias=AliasChoices("score", "total_score"))
    max_score: float | None = Field(
        None, validation_alias=AliasChoices("max_score", "max_points", "out_of")
    )
    percentile: float | None = Field(None, validation_alias=AliasChoices("percentile", "rank"))
    duration_seconds: float | None = Field(
        None, validation_alias=AliasChoices("duration_seconds", "duration", "time_spent_seconds")
    )
    completed_at: datetime | None = Field(
        None, validation_alias=AliasChoices("completed_at", "finished_at", "submitted_at")
    )
    questions: list[CoderbyteQuestion] = Field(
        default_factory=list,
        validation_alias=AliasChoices("questions", "items", "results", "sections"),
    )

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> CoderbyteAssessment:
        """Accepts the envelope either flat or nested under an `assessment` key."""
        body = doc.get("assessment") if isinstance(doc.get("assessment"), dict) else doc
        merged = {**body}
        # Questions are commonly a sibling of `assessment` rather than inside it.
        for key in ("questions", "items", "results", "sections"):
            if key not in merged and isinstance(doc.get(key), list):
                merged[key] = doc[key]
                break
        return cls.model_validate(merged)

    def questions_or_raise(self) -> list[CoderbyteQuestion]:
        if not self.questions:
            raise ValueError(
                "coderbyte payload contains no questions; refusing to extract from an "
                "empty assessment (check the reader service's field names)"
            )
        return self.questions

    def score_line(self) -> str | None:
        if self.score is None:
            return None
        base = f"{self.score:g}/{self.max_score:g}" if self.max_score is not None else f"{self.score:g}"
        return f"{base} ({self.percentile:g}th percentile)" if self.percentile is not None else base
