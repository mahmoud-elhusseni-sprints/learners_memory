"""LMS journey context -> JourneyUpdate."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from learner_memory.integrations.lms.mapping import to_journey_update
from learner_memory.integrations.lms.schemas import LmsJourneyEnvelope
from learner_memory.services.learner_journey_sync import (
    EnrollmentUpdate,
    JourneyUpdate,
    ProgramCounters,
    ProgramProgress,
    ProgressUpdate,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "lms_learner_journey_context.json"
JOURNEY_ID = 1654


def journey_body() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def parse(body: dict):
    return LmsJourneyEnvelope.model_validate(body).data


def test_captured_progress_refresh_maps_to_a_journey_update():
    update = to_journey_update(parse(journey_body()), JOURNEY_ID)

    zero = ProgramCounters(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert update == JourneyUpdate(
        external_id=1654,
        enrollment=EnrollmentUpdate(
            slug="sint-aut-aut-repellendus-ipsam-iusto-aliquid",
            public_url="http://127.0.0.1:8000/journeys/sint-aut-aut-repellendus-ipsam-iusto-aliquid",
            status="in_progress",
            blocked=False,
            manual_added=False,
            started_at=datetime(2026, 9, 16, 2, 18, 5, tzinfo=UTC),
            graduated_at=None,
            source_updated_at=datetime(2026, 9, 16, 2, 18, 5, tzinfo=UTC),
        ),
        progress=ProgressUpdate(
            progress=Decimal("0.0000"),
            programs=(ProgramProgress(external_id=2001668, counters=zero),),
            source_updated_at=datetime(2026, 9, 16, 2, 18, 8, tzinfo=UTC),
        ),
    )


def test_each_lms_counter_lands_in_its_own_completed_column():
    body = journey_body()
    body["data"]["progress"][0]["programs"][0]["counters"] = {
        "files_counter": 1, "videos_counter": 2, "text_lessons_counter": 3,
        "sessions_live_counter": 4, "sessions_record_counter": 5, "codelabs_counter": 6,
        "quizzes_counter": 7, "tasks_counter": 8, "regular_projects_counter": 9,
        "final_projects_counter": 10, "peer_reviews_counter": 11, "ai_interviews_counter": 12,
    }

    counters = to_journey_update(parse(body), JOURNEY_ID).progress.programs[0].counters

    assert counters == ProgramCounters(
        files_completed=1, videos_completed=2, text_lessons_completed=3,
        live_sessions_completed=4, recorded_sessions_completed=5, codelabs_completed=6,
        quizzes_completed=7, tasks_completed=8, regular_projects_completed=9,
        final_projects_completed=10, peer_reviews_completed=11, ai_interviews_completed=12,
    )


@pytest.mark.parametrize(("percent", "fraction"), [
    (0, Decimal("0.0000")),
    (38, Decimal("0.3800")),
    (33.335, Decimal("0.3334")),
    (100, Decimal("1.0000")),
])
def test_percentage_becomes_a_four_place_fraction(percent, fraction):
    body = journey_body()
    body["data"]["progress"][0]["progress_percent"] = percent

    assert to_journey_update(parse(body), JOURNEY_ID).progress.progress == fraction


def test_other_journeys_in_the_answer_are_ignored():
    body = journey_body()
    other = {**body["data"]["enrollments"][0], "journey_id": 999, "status": "graduated"}
    body["data"]["enrollments"].insert(0, other)

    update = to_journey_update(parse(body), JOURNEY_ID)

    assert update.enrollment.status == "in_progress"


def test_missing_parts_map_to_none():
    body = journey_body()
    body["data"]["enrollments"] = []
    body["data"]["progress"] = []

    update = to_journey_update(parse(body), JOURNEY_ID)

    assert (update.enrollment, update.progress) == (None, None)


@pytest.mark.parametrize("bad_percent", [-1, 100.5])
def test_percentage_outside_0_to_100_breaks_the_contract(bad_percent):
    body = journey_body()
    body["data"]["progress"][0]["progress_percent"] = bad_percent

    with pytest.raises(ValidationError):
        parse(body)


def test_negative_counter_breaks_the_contract():
    body = journey_body()
    body["data"]["progress"][0]["programs"][0]["counters"]["videos_counter"] = -1

    with pytest.raises(ValidationError):
        parse(body)
